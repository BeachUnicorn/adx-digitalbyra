"""Sändningsmotorn (README D.2 till D.5, D.8, D.9, J S2 test_s2_tick):
urvalet, tidsfönstret, frysningen, kontrollerna, sms-slingan, återhämtningen,
nödbromsen, taket, demokontot, tillståndsmaskinen och ticken.

Inget når nätet: apps.sms.elks._post är EngineElks (som apps/sms/tests.FakeElks,
med inspelade fel). Tiden är fast (NOW, en tisdag 10.00 i Stockholm) där
tidsfönstret och veckotaket räknas; sms:ens created_at är klockans tid, som
i apps/sms.
"""

import time
from datetime import datetime, timedelta
from unittest import mock

from django.core import mail
from django.db import connections
from django.db.models import Max
from django.http import QueryDict
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount
from apps.projects.models import Customer
from apps.sms import elks, service
from apps.sms.models import SmsAccount, SmsMessage
from apps.sms.pricing import STOCKHOLM
from apps.sms.tests import FakeElks

from . import audience, codes, keys, reports, timing
from . import consent as consents
from .access import Actor, ForeignIds
from .models import (
    CHANNEL_SMS,
    INFORMATION,
    REKLAM,
    AllowedHost,
    Consent,
    ContactList,
    FieldDef,
    LinkCode,
    Recipient,
    Suppression,
    Switchboard,
    Tag,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from .sending import checks, freeze, recover, sms_wrapper, state, tick
from .sending import sms as loop
from .testing import UtskickFixture, enable_utskick, make_contact

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=STOCKHOLM)
BODY = "Hej {förnamn|du}, dags för service hos Exempelrör. Boka: {länk:boka}"
REPLY = "+46766860046"
LIVE = override_settings(
    SMS_SEND_LIVE=True,
    ELKS_API_USERNAME="test",
    ELKS_API_PASSWORD="test-losen",
    SMS_PROVIDER="46elks",
    SMS_CALLBACK_BASE_URL="",
    SMS_RATE_PER_MINUTE=60,
    SMS_GLOBAL_PER_MINUTE=80,
    UTSKICK_SMS_ACCOUNT_PER_MINUTE=45,
    UTSKICK_SMS_GLOBAL_PER_MINUTE=60,
    UTSKICK_REPLY_NUMBER=REPLY,
    INQUIRY_NOTIFICATION_EMAIL="byran@adx.example",
)
STAFF = Actor(label="ADX (Byra)", staff=True)
CUSTOMER = Actor(label="Anna Lindqvist")


class EngineElks(FakeElks):
    """FakeElks med fel i kö: script är en lista med undantag (eller None
    för ett vanligt svar) för de kommande riktiga sändningarna."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.script = []
        self.on_send = None

    def __call__(self, fields):
        if fields.get("dryrun") != "yes":
            if self.on_send is not None:
                self.on_send(fields)
            if self.script:
                error = self.script.pop(0)
                if error is not None:
                    self.calls.append(dict(fields))
                    raise error
        return super().__call__(fields)


def phone(n):
    """PTS fiktiva serie +4670174xxxx."""
    return f"+4670174{n:04d}"


class EngineFixture(UtskickFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sms_account = SmsAccount.objects.create(
            customer=cls.customer, is_enabled=True, sender_name="Exempelror"
        )
        cls.kunder = ContactList.objects.create(account=cls.account, name="Kunder")
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK,
            defaults={
                "sms_enabled": True,
                "links_ready_at": timezone.now(),
                "sms_inbound_ready_at": timezone.now(),
            },
        )

    def setUp(self):
        super().setUp()
        self.fake = EngineElks()
        patcher = mock.patch("apps.sms.elks._post", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.n = 0

    # -- kontakter och utskick ------------------------------------------------

    def person(self, status=consents.YES, account=None, contact_list=None, **data):
        account = account or self.account
        self.n += 1
        data.setdefault("first_name", f"Person{self.n}")
        data.setdefault("phone", phone(1000 + self.n + (500 if account != self.account else 0)))
        kontakt = make_contact(account, **data)
        if status in (consents.YES, consents.EXISTING):
            consents.set_status(
                kontakt,
                CHANNEL_SMS,
                status,
                source=Consent.Source.MANUAL,
                evidence="kassan, 2024",
            )
        elif status != consents.MISSING:
            consents.set_status(kontakt, CHANNEL_SMS, status, source=Consent.Source.MANUAL)
        target = (
            contact_list
            if contact_list is not None
            else (self.kunder if account == self.account else None)
        )
        if target is not None:
            target.memberships.create(contact=kontakt)
        return kontakt

    def people(self, n, **kwargs):
        return [self.person(**kwargs) for _ in range(n)]

    def utskick(self, status=Utskick.Status.FREEZING, body=BODY, account=None, **kwargs):
        account = account or self.account
        kwargs.setdefault(
            "audience",
            {"lists": [self.kunder.pk]} if account == self.account else {},
        )
        u = Utskick.objects.create(
            account=account,
            name="Höstservice värmepump",
            purpose=kwargs.pop("purpose", REKLAM),
            sms_body=body,
            status=status,
            scheduled_at=kwargs.pop("scheduled_at", NOW - timedelta(minutes=1)),
            **kwargs,
        )
        if "{länk:boka}" in body:
            TrackedLink.objects.create(
                account=account,
                utskick=u,
                kind=TrackedLink.Kind.EXTERNAL,
                key="boka",
                destination="https://exempelror.example/boka",
            )
        return u

    def freeze(self, u, now=NOW):
        for _ in range(100):
            result = freeze.freeze_chunk(u, now)
            if result is None or result["done"]:
                break
        u.refresh_from_db()
        return u

    def sending(self, n=3, **kwargs):
        self.people(n)
        u = self.freeze(self.utskick(**kwargs))
        self.assertEqual(u.status, Utskick.Status.SENDING, u.pause_reason)
        return u

    def run_sms(self, now=NOW, seconds=5, only=None):
        with mock.patch("apps.utskick.sending.sms.time.sleep"):
            return loop.send_due(now, time.monotonic() + seconds, only)

    def statuses(self, u):
        return sorted(u.recipients.values_list("status", flat=True))


# ---------------------------------------------------------------------------
# Tidsfönstret (timing.py)
# ---------------------------------------------------------------------------


def at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=STOCKHOLM)


class TimingTests(TestCase):
    def test_easter_and_the_moving_holidays(self):
        self.assertEqual(timing.easter(2026).isoformat(), "2026-04-05")
        self.assertEqual(timing.easter(2027).isoformat(), "2027-03-28")
        days = {d.isoformat() for d in timing.holidays(2026)}
        for day in (
            "2026-01-01",
            "2026-01-06",
            "2026-04-03",  # långfredagen
            "2026-04-06",  # annandag påsk
            "2026-05-01",
            "2026-05-14",  # Kristi himmelsfärd
            "2026-06-06",
            "2026-06-19",  # midsommarafton
            "2026-10-31",  # alla helgons dag
            "2026-12-24",
            "2026-12-31",
        ):
            self.assertIn(day, days)
        self.assertFalse(timing.is_holiday(at(2026, 10, 13, 10).date()))

    def test_weekday_window_and_its_edges(self):
        self.assertFalse(timing.sms_window_open(None, at(2026, 10, 13, 8, 59)))
        self.assertTrue(timing.sms_window_open(None, at(2026, 10, 13, 9, 0)))
        self.assertTrue(timing.sms_window_open(None, at(2026, 10, 13, 19, 59)))
        self.assertFalse(timing.sms_window_open(None, at(2026, 10, 13, 20, 0)))
        self.assertEqual(
            timing.next_window_start(None, at(2026, 10, 13, 20, 0)), at(2026, 10, 14, 9)
        )
        now = at(2026, 10, 13, 12)
        self.assertEqual(timing.next_window_start(None, now), now)

    def test_weekends_and_holidays_use_the_weekend_window(self):
        saturday = at(2026, 10, 17, 9, 30)
        self.assertFalse(timing.sms_window_open(None, saturday))
        self.assertEqual(timing.next_window_start(None, saturday), at(2026, 10, 17, 10))
        christmas_eve = at(2026, 12, 24, 9, 30)  # en torsdag
        self.assertFalse(timing.sms_window_open(None, christmas_eve))
        self.assertEqual(timing.window_for(None, christmas_eve), (10, 18))
        friday_evening = at(2026, 12, 18, 20, 30)
        self.assertEqual(timing.next_window_start(None, friday_evening), at(2026, 12, 19, 10))

    def test_dst_days_count_in_swedish_time(self):
        # Sommartiden börjar 29 mars 2026 och slutar 25 oktober 2026 (söndagar).
        spring = timing.next_window_start(None, at(2026, 3, 29, 5))
        self.assertEqual(spring.astimezone(timezone.UTC).hour, 8)  # 10.00 CEST
        autumn = timing.next_window_start(None, at(2026, 10, 25, 5))
        self.assertEqual(autumn.astimezone(timezone.UTC).hour, 9)  # 10.00 CET
        self.assertTrue(
            timing.sms_window_open(None, datetime(2026, 3, 29, 8, 30, tzinfo=timezone.UTC))
        )
        self.assertFalse(
            timing.sms_window_open(None, datetime(2026, 10, 25, 8, 30, tzinfo=timezone.UTC))
        )

    def test_the_customer_window_is_kept_inside_the_hard_bounds(self):
        row = UtskickSettings(sms_window={"weekday": [6, 23], "weekend": [12, 11]})
        self.assertEqual(timing.window_for(row, at(2026, 10, 13, 10).date()), (8, 21))
        self.assertEqual(timing.window_for(row, at(2026, 10, 17, 10).date()), (10, 18))
        self.assertEqual(timing.window_text(row, at(2026, 10, 13, 10).date()), "08.00 till 21.00")

    def test_texts(self):
        self.assertEqual(timing.next_start_text(None, at(2026, 10, 13, 21)), "09.00 i morgon")
        self.assertEqual(timing.next_start_text(None, at(2026, 10, 13, 7)), "09.00 i dag")
        self.assertEqual(timing.next_start_text(None, at(2026, 10, 13, 12)), "nu")
        self.assertEqual(timing.next_start_text(None, at(2026, 10, 16, 21)), "10.00 i morgon")
        self.assertEqual(
            timing.clock_text(at(2026, 10, 17, 10), at(2026, 10, 13, 12)), "10.00 lördag 17 okt"
        )
        self.assertEqual(
            timing.clock_text(at(2026, 10, 12, 9), at(2026, 10, 13, 12)), "09.00 i går"
        )


# ---------------------------------------------------------------------------
# Urvalet (audience.py)
# ---------------------------------------------------------------------------


class AudienceTests(EngineFixture, TestCase):
    def test_clean_takes_the_form_and_json_and_refuses_foreign_ids(self):
        tag = Tag.objects.create(account=self.account, name="VIP")
        kontakt = self.person()
        form = QueryDict(mutable=True)
        form.setlist("lists", [str(self.kunder.pk)])
        form.setlist("tags", [str(tag.pk)])
        form.setlist("contacts", [str(kontakt.pk)])
        form["exclude_recent"] = "on"
        cleaned = audience.clean(self.account, form)
        self.assertEqual(cleaned["lists"], [self.kunder.pk])
        self.assertEqual(cleaned["exclude"]["recent_days"], 14)
        self.assertEqual(audience.clean(self.account, cleaned), cleaned)
        foreign = ContactList.objects.create(account=self.other_account, name="Deras")
        for bad in (
            {"lists": [foreign.pk]},
            {"exclude": {"lists": [foreign.pk]}},
            {"contacts": ["x"]},
            {"segments": [1]},
            {"exclude": {"recent_days": "många"}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ForeignIds):
                audience.clean(self.account, bad)

    def test_contacts_union_and_excludes(self):
        tag = Tag.objects.create(account=self.account, name="VIP")
        nej = ContactList.objects.create(account=self.account, name="Inte nu")
        a, b = self.people(2)
        c = self.person(contact_list=nej)
        d = self.person(contact_list=nej)
        tag.contacts.add(c)
        self.kunder.memberships.create(contact=d)
        u = self.utskick(
            audience={"lists": [self.kunder.pk], "tags": [tag.pk], "exclude": {"lists": [nej.pk]}}
        )
        self.assertEqual(list(audience.contacts(u)), [a, b])
        u.audience = {}
        self.assertEqual(list(audience.contacts(u)), [])

    def test_count_has_the_i8_shape(self):
        self.people(3)
        self.person(status=consents.DECLINED)
        self.person(status=consents.MISSING)
        u = self.utskick()
        counted = audience.count(u, NOW)
        self.assertEqual(counted["total"], 5)
        self.assertEqual(counted["modes"]["sms_only"], {"sms": 3, "email": 0, "skipped": 2})
        self.assertEqual(counted["skipped_by_reason"], {"declined": 1, "no_consent": 1})
        self.assertEqual((counted["sms"], counted["skipped"]), (3, 2))
        u.audience = {"lists": [self.kunder.pk]}
        self.assertEqual(audience.describe(u), "Lista Kunder")

    def test_information_needs_no_consent_but_never_reaches_a_stop(self):
        self.person(status=consents.MISSING)
        self.person(status=consents.DECLINED)
        stopped = self.person()
        consents.set_status(stopped, CHANNEL_SMS, consents.UNSUBSCRIBED, source=Consent.Source.STOP)
        u = self.utskick(purpose=INFORMATION, info_reason="oppettider")
        counted = audience.count(u, NOW)
        self.assertEqual(counted["sms"], 2)
        self.assertEqual(counted["skipped_by_reason"], {"suppressed": 1})


# ---------------------------------------------------------------------------
# Frysningen (sending/freeze.py, D.3)
# ---------------------------------------------------------------------------


@LIVE
class FreezeTests(EngineFixture, TestCase):
    def test_chunks_freeze_every_contact_once_with_codes(self):
        people = self.people(7)
        self.person(status=consents.DECLINED)
        u = self.utskick()
        with mock.patch.object(audience, "CHUNK", 3):
            first = freeze.freeze_chunk(u, NOW)
            self.assertEqual((first["contacts"], first["queued"], first["done"]), (3, 3, False))
            u.refresh_from_db()
            self.assertEqual(u.freeze_cursor, people[2].pk)
            self.freeze(u)
        self.assertEqual(u.status, Utskick.Status.SENDING)
        self.assertIsNotNone(u.frozen_at)
        self.assertEqual(u.frozen_counts["sms"], 7)
        self.assertEqual(u.frozen_counts["skipped_by_reason"], {"declined": 1})
        queued = u.recipients.filter(status=Recipient.Status.QUEUED)
        self.assertEqual(queued.count(), 7)
        self.assertEqual(queued.first().merge["förnamn"], "Person1")
        link = TrackedLink.objects.get(utskick=u)
        self.assertEqual(LinkCode.objects.filter(link=link, kind=LinkCode.Kind.LINK).count(), 7)
        self.assertFalse(LinkCode.objects.filter(kind=LinkCode.Kind.PERSON).exists())
        # En bit till gör inget: utskicket fryses inte längre.
        self.assertIsNone(freeze.freeze_chunk(u, NOW))
        self.assertEqual(u.recipients.count(), 8)

    def test_a_name_sender_gets_a_person_code(self):
        self.people(2)
        u = self.freeze(
            self.utskick(sms_sender_kind=Utskick.SenderKind.NAME, sms_sender_name="Exempelror")
        )
        self.assertEqual(LinkCode.objects.filter(kind=LinkCode.Kind.PERSON).count(), 2)
        self.assertEqual(u.status, Utskick.Status.SENDING)

    def test_a_failing_chunk_leaves_nothing_behind(self):
        self.people(4)
        u = self.utskick()
        with (
            mock.patch.object(audience, "CHUNK", 2),
            mock.patch.object(freeze, "_verify_codes", side_effect=RuntimeError("pang")),
            self.assertRaises(RuntimeError),
        ):
            freeze.freeze_chunk(u, NOW)
        u.refresh_from_db()
        self.assertEqual(u.freeze_cursor, 0)
        self.assertFalse(u.recipients.exists())
        self.assertFalse(LinkCode.objects.exists())

    def test_a_code_collision_draws_new_codes(self):
        self.person()
        other = self.person(status=consents.MISSING)
        taken = LinkCode.objects.create(
            code="Ab12Cd", kind=LinkCode.Kind.CONFIRM, account=self.account, value_hash="x"
        )
        real = codes.new_codes
        draws = iter([[taken.code], None])

        def new_codes(n):
            drawn = next(draws)
            return drawn if drawn is not None else real(n)

        with mock.patch.object(codes, "new_codes", side_effect=new_codes):
            u = self.freeze(self.utskick())
        self.assertEqual(u.status, Utskick.Status.SENDING)
        code = LinkCode.objects.get(kind=LinkCode.Kind.LINK)
        self.assertNotEqual(code.code, taken.code)
        self.assertTrue(other)

    def test_three_collisions_fail_the_chunk_and_the_next_tick_retries(self):
        self.person()
        LinkCode.objects.create(
            code="Ab12Cd", kind=LinkCode.Kind.CONFIRM, account=self.account, value_hash="x"
        )
        u = self.utskick()
        with (
            mock.patch.object(codes, "new_codes", return_value=["Ab12Cd"]),
            self.assertLogs("apps.utskick.sending.freeze", level="ERROR"),
        ):
            counts = freeze.freeze_due(NOW, time.monotonic() + 5)
        self.assertEqual(counts["failed"], 1)
        self.assertFalse(u.recipients.exists())
        freeze.freeze_due(NOW, time.monotonic() + 5)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.SENDING)
        self.assertEqual(u.recipients.count(), 1)

    def test_a_tampered_audience_yields_no_foreign_recipient(self):
        self.person()
        theirs = ContactList.objects.create(account=self.other_account, name="Deras")
        their_tag = Tag.objects.create(account=self.other_account, name="Deras")
        stranger = self.person(account=self.other_account, contact_list=theirs)
        their_tag.contacts.add(stranger)
        u = self.utskick()
        Utskick.objects.filter(pk=u.pk).update(
            audience={
                "lists": [theirs.pk, self.kunder.pk],
                "tags": [their_tag.pk],
                "contacts": [stranger.pk],
            }
        )
        u = self.freeze(Utskick.objects.get(pk=u.pk))
        self.assertEqual(u.recipients.count(), 1)
        self.assertFalse(u.recipients.filter(contact__account=self.other_account).exists())

    def test_weekly_cap_skips_at_freeze(self):
        kontakt = self.person()
        old = self.utskick(status=Utskick.Status.SENT, audience={"contacts": []})
        for _ in range(2):
            Recipient.objects.create(
                utskick=Utskick.objects.create(
                    account=self.account, name="Förra", status=Utskick.Status.SENT
                ),
                contact=kontakt,
                channel=CHANNEL_SMS,
                address=kontakt.phone,
                status=Recipient.Status.DELIVERED,
                sent_at=NOW - timedelta(days=1),
            )
        self.assertTrue(old)
        u = self.freeze(self.utskick())
        self.assertEqual(self.statuses(u), [Recipient.Status.SKIPPED])
        self.assertEqual(u.recipients.get().skip_reason, Recipient.SkipReason.WEEKLY_CAP)

    def test_late_starts_pause(self):
        self.person()
        late = self.utskick(status=Utskick.Status.SCHEDULED, scheduled_at=NOW - timedelta(hours=4))
        overnight = self.utskick(
            status=Utskick.Status.SCHEDULED, scheduled_at=at(2026, 10, 12, 23, 30)
        )
        due = self.utskick(status=Utskick.Status.SCHEDULED, scheduled_at=NOW - timedelta(hours=1))
        later = self.utskick(status=Utskick.Status.SCHEDULED, scheduled_at=NOW + timedelta(hours=1))
        self.assertEqual(freeze.start_due(NOW), {"started": 1, "late": 2, "content": 0, "held": 0})
        for u, status, reason in (
            (late, Utskick.Status.PAUSED, Utskick.PauseReason.LATE),
            (overnight, Utskick.Status.PAUSED, Utskick.PauseReason.LATE),
            (due, Utskick.Status.FREEZING, ""),
            (later, Utskick.Status.SCHEDULED, ""),
        ):
            u.refresh_from_db()
            self.assertEqual((u.status, u.pause_reason), (status, reason))

    def test_low_disk_keeps_it_scheduled_and_alerts(self):
        self.person()
        u = self.utskick(status=Utskick.Status.SCHEDULED)
        with mock.patch.object(freeze, "free_disk", return_value=0.05):
            counts = freeze.start_due(NOW)
        self.assertEqual(counts["held"], 1)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.SCHEDULED)
        self.assertEqual(len(mail.outbox), 1)

    def test_prechecks_pause_at_the_cap_before_the_first_sms(self):
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(monthly_cap_kr=1)
        self.people(3)
        u = self.freeze(self.utskick())
        self.assertEqual(
            (u.status, u.pause_reason),
            (Utskick.Status.PAUSED_CAP, Utskick.PauseReason.SMS_COST_CAP),
        )
        self.assertEqual(u.frozen_counts["estimate"]["sms"], 3)
        self.assertEqual(self.fake.calls, [])

    def test_prechecks_pause_when_the_audience_grew(self):
        self.people(5)
        u = self.utskick(confirm_summary={"sms": 3, "email": 0})
        u = self.freeze(u)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.AUDIENCE_GREW)

    def test_first_big_utskick_and_information_alert_the_agency(self):
        self.people(3)
        with mock.patch.object(freeze, "FIRST_BIG", 2), mock.patch.object(freeze, "INFO_BIG", 2):
            self.freeze(self.utskick(purpose=INFORMATION, info_reason="oppettider"))
            self.freeze(self.utskick())
        subjects = [m.subject for m in mail.outbox]
        self.assertIn("Utskick: första stora utskicket för en kund", subjects)
        self.assertIn("Utskick: informationsutskick att granska", subjects)
        self.assertEqual(subjects.count("Utskick: första stora utskicket för en kund"), 1)
        for message in mail.outbox:
            self.assertNotIn("+4670", message.body)


# ---------------------------------------------------------------------------
# Kontrollerna (sending/checks.py)
# ---------------------------------------------------------------------------


@LIVE
class CheckTests(EngineFixture, TestCase):
    def claimed(self, u):
        r = u.recipients.filter(status=Recipient.Status.QUEUED).first()
        Recipient.objects.filter(pk=r.pk).update(status=Recipient.Status.SENDING)
        r.refresh_from_db()
        return r

    def test_deferring_checks(self):
        u = self.sending(1)
        r = self.claimed(u)
        self.assertTrue(checks.send_time_checks(r, NOW).ok)
        self.assertEqual(checks.send_time_checks(r, NOW).sender, REPLY)
        closed = checks.send_time_checks(r, at(2026, 10, 13, 21))
        self.assertEqual((closed.defer, closed.reason), (True, "window"))
        self.assertEqual(closed.not_before, at(2026, 10, 14, 9))
        Switchboard.objects.update(sms_paused_until=NOW + timedelta(minutes=5))
        self.assertEqual(checks.send_time_checks(r, NOW).reason, "breaker")
        Switchboard.objects.update(sms_paused_until=None, sms_enabled=False)
        self.assertEqual(checks.send_time_checks(r, NOW).reason, "sms_off")
        Switchboard.objects.update(sms_enabled=True)
        UtskickSettings.objects.filter(pk=self.settings.pk).update(sending_blocked=True)
        self.assertEqual(checks.send_time_checks(r, NOW).reason, "blocked")
        UtskickSettings.objects.filter(pk=self.settings.pk).update(sending_blocked=False)
        Customer.objects.filter(pk=self.customer.pk).update(is_active=False)
        self.assertEqual(checks.send_time_checks(r, NOW).reason, "account_disabled")

    def test_per_person_checks_read_fresh(self):
        u = self.sending(4)
        rows = list(u.recipients.order_by("pk"))
        contacts = [r.contact for r in rows]
        consents.set_status(
            contacts[0], CHANNEL_SMS, consents.DECLINED, source=Consent.Source.MANUAL
        )
        Suppression.objects.create(
            account=self.account,
            channel=CHANNEL_SMS,
            value_hash=keys.value_hash(CHANNEL_SMS, contacts[1].phone),
            reason=Suppression.Reason.STOP,
        )
        type(contacts[2]).objects.filter(pk=contacts[2].pk).update(phone=phone(9999))
        contacts[3].delete()
        expected = ["declined", "suppressed", "address_changed", "deleted"]
        for r, reason in zip(rows, expected, strict=True):
            r.refresh_from_db()
            check = checks.send_time_checks(r, NOW)
            self.assertEqual((check.skip, check.reason), (True, reason))

    def test_reply_collision_uses_the_name_sender_or_skips(self):
        u = self.sending(1)
        r = self.claimed(u)
        other_sms = SmsAccount.objects.create(customer=self.other_customer, is_enabled=True)
        SmsMessage.objects.create(
            account=other_sms,
            source="utskick",
            status=SmsMessage.Status.SENT,
            to=r.address,
            sender=REPLY,
            body="Hej",
            created_at=timezone.now() - timedelta(days=3),
        )
        check = checks.send_time_checks(r, NOW)
        self.assertEqual((check.sender, check.collided), ("Exempelror", True))
        self.assertEqual(checks.collision_count(u, NOW), 0)  # mottagaren är inte i kö
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(sender_name="")
        check = checks.send_time_checks(r, NOW)
        self.assertEqual((check.skip, check.reason), (True, "reply_collision"))

    def test_collision_count_before_and_after_the_freeze(self):
        kontakt = self.person()
        other_sms = SmsAccount.objects.create(customer=self.other_customer, is_enabled=True)
        SmsMessage.objects.create(
            account=other_sms,
            status=SmsMessage.Status.SENT,
            to=kontakt.phone,
            sender=REPLY,
            body="Hej",
        )
        u = self.utskick(status=Utskick.Status.DRAFT)
        self.assertEqual(checks.collision_count(u, NOW), 1)

    def test_reply_checks_for_the_inbox(self):
        kontakt = self.person()
        self.assertTrue(checks.reply_checks(self.account, kontakt, kontakt.phone, NOW).ok)
        consents.set_status(kontakt, CHANNEL_SMS, consents.UNSUBSCRIBED, source=Consent.Source.STOP)
        check = checks.reply_checks(self.account, None, kontakt.phone, NOW)
        self.assertEqual((check.skip, check.text), (True, checks.SUPPRESSED_TEXT))
        Switchboard.objects.update(sms_paused_until=NOW + timedelta(minutes=5))
        self.assertEqual(
            checks.reply_checks(self.account, kontakt, kontakt.phone, NOW).text, checks.BREAKER_TEXT
        )

    def test_sender_identified(self):
        u = self.utskick(status=Utskick.Status.DRAFT)
        self.assertTrue(checks.sender_identified(u))
        self.assertFalse(checks.sender_identified(u, "Hej, välkommen in."))
        u.sms_sender_kind = Utskick.SenderKind.NAME
        self.assertTrue(checks.sender_identified(u, "Hej, välkommen in."))

    def test_information_rules(self):
        u = self.utskick(status=Utskick.Status.DRAFT, purpose=INFORMATION, body="Vi har stängt.")
        self.assertEqual(checks.information_problems(u), [checks.INFO_REASON_TEXT])
        u.info_reason = "oppettider"
        self.assertEqual(checks.information_problems(u), [])
        for text in (
            "Nu 20 % rabatt",
            "Spara 200 kr",
            "Använd koden VINTER",
            "Rea i helgen",
            "99:-",
        ):
            with self.subTest(text=text):
                u.sms_body = text
                self.assertEqual(checks.information_problems(u), [checks.LOOKS_LIKE_AD_TEXT])
        u.sms_body = "Området vid rean är stängt"  # "rean" som ord
        self.assertEqual(checks.information_problems(u), [checks.LOOKS_LIKE_AD_TEXT])
        u.sms_body = "Vi har nya öppettider i området."
        self.assertEqual(checks.information_problems(u), [])
        u.sms_body = "Nu 20 % rabatt"
        u.content_override = {"by": "ADX", "reason": "Lagstadgad prisinformation"}
        # Ett undantag utan text att gälla för släpper ingenting.
        self.assertEqual(checks.information_problems(u), [checks.LOOKS_LIKE_AD_TEXT])
        u.content_override["fingerprint"] = checks.content_fingerprint(u)
        self.assertEqual(checks.information_problems(u), [])
        u.content_override = {}
        u.sms_body = "Läs mer: {länk:boka}"
        TrackedLink.objects.create(
            account=self.account,
            utskick=u,
            kind=TrackedLink.Kind.EXTERNAL,
            key="boka",
            destination="https://annan.example/boka",
        )
        self.assertEqual(
            checks.information_problems(u),
            [checks.INFO_HOST_TEXT.format(host="annan.example")],
        )
        # Adressen kunden själv skrev i Flamingo gör ingen värd till kundens egen.
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            website_url="https://www.annan.example/"
        )
        u.account.refresh_from_db()
        self.assertEqual(
            checks.information_problems(u),
            [checks.INFO_HOST_TEXT.format(host="annan.example")],
        )
        # Webbplatsen i ADX kundregister gör det.
        Customer.objects.filter(pk=self.customer.pk).update(website="https://www.annan.example/")
        u.account = FlamingoAccount.objects.select_related("customer").get(pk=self.account.pk)
        self.assertEqual(checks.information_problems(u), [])

    def test_information_checks_fallbacks_and_field_values(self):
        """Säkerhetsgranskningen S2: reklamorden prövades bara i texten utanför
        klamrarna. Reservtexterna och fältvärdena prövas också."""
        u = self.utskick(
            status=Utskick.Status.DRAFT,
            purpose=INFORMATION,
            info_reason="oppettider",
            body="Hej {förnamn|halva priset med koden VINTER}. Nya öppettider hos Exempelrör.",
        )
        self.assertEqual(checks.information_problems(u), [checks.LOOKS_LIKE_AD_TEXT])
        u.sms_body = "Hej {förnamn}. Nya öppettider hos Exempelrör."
        self.assertEqual(checks.information_problems(u), [])
        u.merge_fallbacks = {"förnamn": "50 % rabatt"}
        self.assertEqual(checks.information_problems(u), [checks.LOOKS_LIKE_AD_TEXT])
        u.merge_fallbacks = {}
        FieldDef.objects.create(account=self.account, key="tid", label="Tid")
        u.sms_body = "Din tid: {fält:tid}. Hälsningar Exempelrör."
        u.save()
        self.person(fields={"tid": "14 nov 10.00"})
        self.assertEqual(checks.information_problems(u), [])
        self.person(fields={"tid": "Halva priset med koden VINTER"})
        self.assertEqual(checks.information_problems(u), [checks.INFO_FIELD_TEXT.format(key="tid")])
        # Byråns undantag släpper inte fältvärdena: en import kan ändra dem.
        u.content_override = {"reason": "x", "fingerprint": checks.content_fingerprint(u)}
        self.assertEqual(checks.information_problems(u), [checks.INFO_FIELD_TEXT.format(key="tid")])
        # Efter frysningen prövas mottagarnas frysta värden.
        u.content_override = {}
        u.save()
        Utskick.objects.filter(pk=u.pk).update(status=Utskick.Status.FREEZING)
        self.freeze(u)
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), (Utskick.Status.PAUSED, "content"))
        self.assertEqual(checks.information_problems(u), [checks.INFO_FIELD_TEXT.format(key="tid")])


# ---------------------------------------------------------------------------
# Sms-slingan (sending/sms.py, D.4)
# ---------------------------------------------------------------------------


@LIVE
class SmsLoopTests(EngineFixture, TestCase):
    def test_sends_each_recipient_once_with_its_reference(self):
        u = self.sending(3)
        counts = self.run_sms()
        self.assertEqual(counts["sent"], 3)
        self.assertEqual(self.statuses(u), [Recipient.Status.SENT] * 3)
        self.assertEqual(len(self.fake.sends), 3)
        messages = SmsMessage.objects.filter(source="utskick").order_by("pk")
        self.assertEqual(
            sorted(m.reference for m in messages),
            sorted(f"~u{u.pk}:{r.pk}" for r in u.recipients.all()),
        )
        for r in u.recipients.select_related("sms_message"):
            self.assertEqual(r.sms_message.sender, REPLY)
            self.assertEqual(r.sms_sender, REPLY)
            self.assertIn("Svara STOPP", r.sms_message.body)
            self.assertRegex(r.sms_message.body, r"k\.\S+/[A-Za-z0-9]{6}")
            self.assertGreater(r.parts, 0)
        # En omgång till skickar inget.
        self.run_sms()
        self.assertEqual(len(self.fake.sends), 3)

    def test_throughput_is_45_per_account_and_minute(self):
        u = self.sending(50)
        with mock.patch.object(loop, "PACE_SLEEP", 0.2):
            counts = loop.send_due(NOW, time.monotonic() + 6)
        self.assertEqual(counts["sent"], 45)
        self.assertEqual(len(self.fake.sends), 45)
        self.assertEqual(u.recipients.filter(status=Recipient.Status.QUEUED).count(), 5)
        self.assertGreaterEqual(counts["paced"], 1)

    def test_the_global_budget_counts_flamingo_sms(self):
        from apps.flamingo.models import SmsLog

        u = self.sending(3)
        for _ in range(58):
            SmsLog.objects.create(
                account=self.account, kind=SmsLog.KIND_OWNER, body="x", status=SmsLog.STATUS_SENT
            )
        self.assertEqual(loop.budget_for(self.sms_account), 2)
        # Gott om tid för de två sms:en (en tung körning av hela sviten
        # hann annars bara ett), och slingan väntar inte på en ledig plats
        # när budgeten är slut (PACE_SLEEP längre än tiden som är kvar).
        with mock.patch.object(loop, "PACE_SLEEP", 100):
            self.run_sms(seconds=10)
        self.assertEqual(u.recipients.filter(status=Recipient.Status.SENT).count(), 2)

    def test_a_pause_mid_batch_then_resume_sends_everyone_once(self):
        u = self.sending(6)

        def pause_after_two(fields):
            if len(self.fake.sends) == 2:
                state.pause(u, Utskick.PauseReason.CUSTOMER, actor=CUSTOMER)

        self.fake.on_send = pause_after_two
        self.run_sms()
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED)
        self.assertEqual(u.recipients.filter(status=Recipient.Status.SENT).count(), 3)
        self.assertEqual(u.recipients.filter(status=Recipient.Status.QUEUED).count(), 3)
        self.assertFalse(u.recipients.filter(status=Recipient.Status.SENDING).exists())
        self.assertEqual(u.recipients.aggregate(m=Max("attempts"))["m"], 1)
        self.fake.on_send = None
        result = state.resume(u, actor=CUSTOMER)
        self.assertTrue(result.ok, result.error)
        self.run_sms()
        self.assertEqual(self.statuses(u), [Recipient.Status.SENT] * 6)
        self.assertEqual(len(self.fake.sends), 6)
        self.assertEqual(len({c["to"] for c in self.fake.sends}), 6)

    def test_window_and_switchboard_defer_without_sending(self):
        u = self.sending(2)
        evening = at(2026, 10, 13, 20, 30)
        counts = self.run_sms(now=evening)
        self.assertEqual(counts.get("window"), 2)
        self.assertEqual(self.fake.sends, [])
        self.assertEqual(
            set(u.recipients.values_list("not_before", flat=True)), {at(2026, 10, 14, 9)}
        )
        self.assertFalse(tick.sms_due(evening).exists())
        Switchboard.objects.update(sms_enabled=False)
        self.run_sms(now=at(2026, 10, 14, 9, 5))
        self.assertEqual(self.fake.sends, [])
        Switchboard.objects.update(sms_enabled=True)
        self.run_sms(now=at(2026, 10, 14, 9, 5))
        self.assertEqual(len(self.fake.sends), 2)

    def test_consent_withdrawn_between_freeze_and_send_is_skipped(self):
        u = self.sending(2)
        first = u.recipients.order_by("pk").first()
        consents.set_status(
            first.contact, CHANNEL_SMS, consents.DECLINED, source=Consent.Source.PREFERENCE
        )
        self.run_sms()
        first.refresh_from_db()
        self.assertEqual((first.status, first.skip_reason), ("skipped", "declined"))
        self.assertEqual(len(self.fake.sends), 1)

    def test_weekly_cap_at_send_time(self):
        u = self.sending(1)
        r = u.recipients.get()
        for name in ("Förra", "Förrförra"):
            other = Utskick.objects.create(account=self.account, name=name, status="sent")
            Recipient.objects.create(
                utskick=other,
                contact=r.contact,
                channel=CHANNEL_SMS,
                address=r.address,
                status="delivered",
                sent_at=NOW - timedelta(hours=2),
            )
        self.run_sms()
        r.refresh_from_db()
        self.assertEqual(r.skip_reason, "weekly_cap")
        self.assertEqual(self.fake.sends, [])

    def test_a_collision_switches_to_the_name_sender_with_an_unsubscribe_link(self):
        u = self.sending(1)
        r = u.recipients.get()
        other_sms = SmsAccount.objects.create(customer=self.other_customer, is_enabled=True)
        SmsMessage.objects.create(
            account=other_sms, status="sent", to=r.address, sender=REPLY, body="Hej"
        )
        self.run_sms()
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.SENT)
        self.assertEqual(r.sms_sender, "Exempelror")
        self.assertEqual(self.fake.sends[0]["from"], "Exempelror")
        self.assertNotIn("Svara STOPP", self.fake.sends[0]["message"])
        code = LinkCode.objects.get(recipient=r, kind=LinkCode.Kind.PERSON)
        self.assertIn(f"/s/{code.code}", self.fake.sends[0]["message"])

    def test_rate_limited_requeues_and_the_retry_reaches_46elks(self):
        u = self.sending(1)
        r = u.recipients.get()
        self.fake.script = [elks.ElksError("46elks svarade 429", 429, throttled=True)]
        counts = self.run_sms()
        self.assertEqual(counts["rate_limited"], 1)
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.QUEUED)
        self.assertGreater(r.not_before, timezone.now() + timedelta(seconds=50))
        self.assertEqual(r.attempts, 0)
        self.assertEqual(mail.outbox, [])
        # Nästa tick (NOW ligger efter not_before): samma reference, nu till 46elks.
        self.run_sms()
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.SENT)
        throttled, sent = SmsMessage.objects.filter(reference=f"~u{u.pk}:{r.pk}").order_by("pk")
        self.assertEqual((throttled.status, throttled.error_code), ("rejected", "rate_limited"))
        self.assertEqual(sent.status, "sent")
        self.assertEqual(len(self.fake.sends), 2)

    def test_the_cap_pauses_every_sending_utskick_once(self):
        self.people(3)
        first = self.freeze(self.utskick())
        second = self.freeze(self.utskick())
        self.assertEqual(second.status, Utskick.Status.SENDING)
        # Ett sms ryms (52 öre + 5 öre), nästa inte.
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(monthly_cap_kr=1)
        counts = self.run_sms()
        self.assertEqual(counts["cap"], 1)
        for u in (first, second):
            u.refresh_from_db()
            self.assertEqual((u.status, u.pause_reason), ("paused_cap", "sms_cost_cap"))
        self.assertEqual(SmsMessage.objects.filter(status="blocked_cap").count(), 1)
        blocked = SmsMessage.objects.get(status="blocked_cap")
        self.assertFalse(Recipient.objects.filter(status=Recipient.Status.SENDING).exists())
        # Taket höjs: fortsätt, och samma reference går nu till 46elks.
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(monthly_cap_kr=500)
        for u in (first, second):
            self.assertTrue(state.resume(u, actor=CUSTOMER).ok)
        self.run_sms()
        retried = SmsMessage.objects.filter(reference=blocked.reference).exclude(pk=blocked.pk)
        self.assertEqual(retried.get().status, "sent")
        self.assertEqual(Recipient.objects.filter(status=Recipient.Status.SENT).count(), 6)

    def test_sms_not_enabled_pauses_the_accounts_sending_utskick(self):
        u = self.sending(1)
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(is_enabled=False)
        self.run_sms()
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), ("paused", "sms_disabled"))
        self.assertEqual(self.statuses(u), [Recipient.Status.QUEUED])

    def test_sender_not_allowed_fails_and_pauses_for_provider(self):
        u = self.sending(2, sms_sender_kind=Utskick.SenderKind.NAME, sms_sender_name="Annat")
        self.run_sms()
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), ("paused", "provider"))
        self.assertIn(Recipient.Status.FAILED, self.statuses(u))
        self.assertEqual(self.fake.sends, [])
        self.assertTrue(any("provider" in m.subject for m in mail.outbox))

    def test_invalid_numbers_fail_with_their_reason(self):
        u = self.sending(1)
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(allowed_countries=["NO"])
        self.run_sms()
        r = u.recipients.get()
        self.assertEqual((r.status, r.skip_reason), ("failed", "country"))

    def test_the_circuit_breaker_stops_all_sms_after_three_troubles(self):
        u = self.sending(5)
        self.fake.script = [elks.ElksError("46elks svarade 502", 502, ambiguous=True)] * 3
        counts = self.run_sms()
        self.assertEqual(counts["unknown"], 3)
        row = Switchboard.get_solo()
        self.assertGreater(row.sms_paused_until, timezone.now())
        self.assertEqual(u.recipients.filter(status=Recipient.Status.UNKNOWN).count(), 3)
        self.assertEqual(u.recipients.filter(status=Recipient.Status.QUEUED).count(), 2)
        subjects = [m.subject for m in mail.outbox]
        self.assertEqual(subjects.count("Utskick: sms pausade efter fel hos 46elks"), 1)
        before = len(self.fake.calls)
        self.run_sms()
        self.assertEqual(len(self.fake.calls), before)

    def test_five_provider_errors_in_a_row_pause_the_utskick(self):
        u = self.sending(6)
        self.fake.script = [elks.ElksError("46elks svarade 400", 400)] * 6
        with mock.patch.object(checks, "BREAKER_COUNT", 100):
            self.run_sms()
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), ("paused", "provider"))
        self.assertEqual(u.recipients.filter(status=Recipient.Status.FAILED).count(), 5)

    def test_a_delivery_report_moves_recipients_forward_only(self):
        u = self.sending(1)
        self.run_sms()
        r = u.recipients.select_related("sms_message").get()
        message = r.sms_message
        code, _ = service.apply_delivery_report(message.pk, message.provider_id, "delivered")
        self.assertEqual(code, 200)
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.DELIVERED)
        service.apply_delivery_report(message.pk, message.provider_id, "sent")
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.DELIVERED)
        # Slingans egen skrivning efter en rapport som kom först gör inget bakåt.
        loop.adopt(r, SmsMessage.objects.get(pk=message.pk), REPLY)
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.DELIVERED)

    def test_only_limits_the_loop_to_one_utskick(self):
        self.people(2)
        first = self.freeze(self.utskick())
        second = self.freeze(self.utskick())
        self.run_sms(only=second.pk)
        self.assertEqual(self.statuses(first), [Recipient.Status.QUEUED] * 2)
        self.assertEqual(self.statuses(second), [Recipient.Status.SENT] * 2)


# ---------------------------------------------------------------------------
# Demokontot (D12)
# ---------------------------------------------------------------------------


def refuse(*args, **kwargs):
    raise AssertionError("Demokontot nådde 46elks.")


@LIVE
class DemoTests(EngineFixture, TestCase):
    def test_the_demo_is_simulated_without_46elks_or_sms_rows(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        u = self.sending(3)
        Switchboard.objects.update(sms_enabled=False)
        with mock.patch("apps.sms.elks._post", side_effect=refuse):
            counts = self.run_sms()
        self.assertEqual(counts["simulated"], 3)
        self.assertEqual(self.statuses(u), [Recipient.Status.DELIVERED] * 3)
        self.assertTrue(all(u.recipients.values_list("simulated", flat=True)))
        self.assertFalse(SmsMessage.objects.exists())
        tick.finish(NOW)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.SENT)

    def test_every_path_to_the_wrapper_refuses_the_demo(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        with self.assertRaises(sms_wrapper.DemoRefused):
            sms_wrapper.send(self.account, to=phone(1), body="Hej", sender=REPLY, source="utskick")
        self.assertFalse(checks.reply_checks(self.account, None, phone(1), NOW).ok)
        with self.assertRaises(ValueError):
            loop.simulate(self.other_account, NOW)


# ---------------------------------------------------------------------------
# Återhämtningen (sending/recover.py, D.5)
# ---------------------------------------------------------------------------


@LIVE
class RecoverTests(EngineFixture, TestCase):
    def stale(self, u, r):
        Recipient.objects.filter(pk=r.pk).update(
            status=Recipient.Status.SENDING,
            claimed_at=timezone.now() - timedelta(minutes=10),
            attempts=1,
        )

    def message(self, u, r, **kwargs):
        values = {
            "account": self.sms_account,
            "source": "utskick",
            "to": r.address,
            "sender": REPLY,
            "body": "Hej",
            "reference": f"~u{u.pk}:{r.pk}",
            "parts": 1,
        }
        values.update(kwargs)
        return SmsMessage.objects.create(**values)

    def test_a_reserved_row_becomes_unknown_and_is_never_sent_again(self):
        u = self.sending(1)
        r = u.recipients.get()
        self.stale(u, r)
        msg = self.message(u, r, status="reserved", error_code="provider_unknown")
        self.assertTrue(recover.stale_exists())
        self.assertEqual(recover.recover_stale(), {"unknown": 1})
        r.refresh_from_db()
        self.assertEqual((r.status, r.sms_message_id), ("unknown", msg.pk))
        self.run_sms()
        self.assertEqual(self.fake.sends, [])

    def test_a_sent_row_is_adopted(self):
        u = self.sending(1)
        r = u.recipients.get()
        self.stale(u, r)
        self.message(u, r, status="delivered", sent_at=timezone.now())
        self.assertEqual(recover.recover_stale(), {"adopted": 1})
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.DELIVERED)

    def test_only_the_row_holding_the_reference_is_adopted(self):
        u = self.sending(1)
        r = u.recipients.get()
        self.stale(u, r)
        self.message(u, r, status="rejected", error_code="rate_limited")
        self.message(u, r, status="failed", error_code="provider_error")
        self.assertEqual(recover.recover_stale(), {"requeued": 1})
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.QUEUED)
        self.run_sms()
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.SENT)
        self.assertEqual(len(self.fake.sends), 1)

    def test_a_poison_recipient_fails_after_max_attempts(self):
        u = self.sending(1)
        r = u.recipients.get()
        self.stale(u, r)
        Recipient.objects.filter(pk=r.pk).update(attempts=loop.MAX_ATTEMPTS)
        self.assertEqual(recover.recover_stale(), {"failed": 1})

    def test_fresh_claims_are_left_alone(self):
        u = self.sending(1)
        Recipient.objects.filter(utskick=u).update(
            status=Recipient.Status.SENDING, claimed_at=timezone.now()
        )
        self.assertFalse(recover.stale_exists())
        self.assertEqual(recover.recover_stale(), {})


# ---------------------------------------------------------------------------
# Tillståndsmaskinen (sending/state.py)
# ---------------------------------------------------------------------------


@LIVE
class StateTests(EngineFixture, TestCase):
    def draft(self, **kwargs):
        u = self.utskick(
            status=Utskick.Status.DRAFT, scheduled_at=NOW + timedelta(days=1), **kwargs
        )
        state.issue_nonce(u)
        return u

    def test_confirm_needs_the_nonce_once_and_records_the_actor(self):
        self.people(2)
        u = self.draft()
        actor = Actor(user=self.staff, label="ADX (byra)", staff=True)
        stale = state.confirm(u, actor=actor, nonce="fel", summary={"sms": 2}, now=NOW)
        self.assertEqual((stale.ok, stale.error), (False, state.STALE_TEXT))
        nonce = u.confirm_nonce
        result = state.confirm(u, actor=actor, nonce=nonce, summary={"sms": 2}, now=NOW)
        self.assertTrue(result.ok, result.error)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.SCHEDULED)
        self.assertEqual((u.confirmed_by, u.confirmed_as_staff), (self.staff, True))
        self.assertEqual(u.confirm_summary, {"sms": 2})
        self.assertEqual(u.confirm_nonce, "")
        again = state.confirm(u, actor=actor, nonce=nonce, summary={}, now=NOW)
        self.assertFalse(again.ok)

    def test_confirm_refuses_without_the_switch_and_in_the_past(self):
        self.person()
        u = self.draft()
        Switchboard.objects.update(sms_enabled=False)
        result = state.confirm(u, actor=CUSTOMER, nonce=u.confirm_nonce, summary={}, now=NOW)
        self.assertEqual(result.error, checks.SMS_OFF_TEXT)
        Switchboard.objects.update(sms_enabled=True)
        Utskick.objects.filter(pk=u.pk).update(scheduled_at=NOW - timedelta(hours=1))
        u.refresh_from_db()
        result = state.confirm(u, actor=CUSTOMER, nonce=u.confirm_nonce, summary={}, now=NOW)
        self.assertEqual(result.error, state.PAST_TEXT)
        result = state.confirm(
            u, actor=CUSTOMER, nonce=u.confirm_nonce, summary={}, send_now=True, now=NOW
        )
        self.assertTrue(result.ok)
        self.assertEqual(u.scheduled_at, NOW)

    def test_unconfirm_and_cancel(self):
        self.person()
        u = self.draft()
        state.confirm(u, actor=CUSTOMER, nonce=u.confirm_nonce, summary={}, now=NOW)
        self.assertTrue(state.unconfirm(u))
        u.refresh_from_db()
        self.assertEqual((u.status, u.confirmed_at), (Utskick.Status.DRAFT, None))
        sending = self.sending(1)
        self.assertFalse(state.cancel(sending, actor=CUSTOMER))  # pågående: pausa först
        state.pause(sending, Utskick.PauseReason.CUSTOMER, actor=CUSTOMER)
        self.assertTrue(state.cancel(sending, actor=CUSTOMER))
        self.assertEqual(set(self.statuses(sending)), {Recipient.Status.CANCELLED})
        self.assertEqual(sending.stats["cancelled"]["by"], CUSTOMER.label)
        self.assertIsNotNone(sending.finished_at)

    def test_resume_rules(self):
        u = self.sending(1)
        state.pause(u, Utskick.PauseReason.STOPS, note="9 av 380")
        self.assertEqual(state.resume(u, actor=CUSTOMER).error, state.STAFF_RESUMES_TEXT)
        self.assertTrue(state.resume(u, actor=STAFF).ok)
        state.pause(u, Utskick.PauseReason.LATE)
        self.assertEqual(state.resume(u, actor=STAFF).error, state.RECONFIRM_TEXT)

    def test_a_reconfirmed_frozen_utskick_goes_straight_to_sending(self):
        self.people(5)
        u = self.freeze(self.utskick(confirm_summary={"sms": 2}))
        self.assertEqual(u.pause_reason, Utskick.PauseReason.AUDIENCE_GREW)
        state.issue_nonce(u)
        result = state.confirm(
            u, actor=CUSTOMER, nonce=u.confirm_nonce, summary={"sms": 5}, send_now=True, now=NOW
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(u.status, Utskick.Status.SENDING)

    def card(self, **data):
        from django.test import Client
        from django.urls import reverse

        client = Client()
        client.force_login(self.staff)
        url = reverse("manage:utskick_customer_update", args=[self.customer.pk])
        return client.post(url, {"display_name": "Exempelrör", **data})

    def test_turning_utskick_off_on_the_card_pauses_everything(self):
        sending = self.sending(1)
        scheduled = self.utskick(
            status=Utskick.Status.SCHEDULED, scheduled_at=NOW + timedelta(days=1)
        )
        draft = self.utskick(status=Utskick.Status.DRAFT)
        self.assertEqual(self.card().status_code, 302)
        for u, status in (
            (sending, Utskick.Status.PAUSED),
            (scheduled, Utskick.Status.PAUSED),
            (draft, Utskick.Status.DRAFT),
        ):
            u.refresh_from_db()
            self.assertEqual(u.status, status)
        self.assertEqual(sending.pause_reason, Utskick.PauseReason.ACCOUNT_DISABLED)
        self.assertEqual(mail.outbox, [])
        # Att slå på igen fortsätter inget av sig självt.
        self.card(is_enabled="on")
        sending.refresh_from_db()
        self.assertEqual(sending.status, Utskick.Status.PAUSED)
        self.assertEqual(state.resume(sending, actor=CUSTOMER).error, state.RECONFIRM_TEXT)

    def test_stopping_sending_on_the_card_pauses_as_blocked(self):
        sending = self.sending(1)
        self.card(is_enabled="on", sending_blocked="on", blocked_reason="Klagomål på numret")
        sending.refresh_from_db()
        self.assertEqual((sending.status, sending.pause_reason), ("paused", "blocked"))

    def test_ending_utskick_removes_the_s2_rows(self):
        from .manage_views import end_account

        sending = self.sending(2)
        self.run_sms()
        self.assertTrue(LinkCode.objects.exists())
        end_account(self.account, self.staff)
        self.assertFalse(Utskick.objects.filter(pk=sending.pk).exists())
        self.assertFalse(Recipient.objects.exists())
        self.assertFalse(LinkCode.objects.filter(account=self.account).exists())
        self.assertEqual(SmsMessage.objects.filter(source="utskick").count(), 2)

    def test_ending_utskick_removes_the_reply_leads_too(self):
        """Säkerhetsgranskningen S2: förfrågan bakom en svarstråd (numret och
        senaste svaret) stod kvar i Inkorgen."""
        from apps.flamingo.models import Lead

        from .manage_views import end_account

        lead = Lead.objects.create(
            account=self.account,
            source=Lead.SOURCE_REPLY,
            phone="070-174 06 01",
            message="Hej, kan ni komma på tisdag?",
        )
        other = Lead.objects.create(account=self.account, name="Från sidan")
        end_account(self.account, self.staff)
        self.assertFalse(Lead.objects.filter(pk=lead.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=other.pk).exists())

    def test_a_disabled_account_still_takes_a_stop(self):
        kontakt = self.person()
        UtskickSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        from . import suppression

        suppression.suppress(
            self.account,
            CHANNEL_SMS,
            kontakt.phone,
            reason=Suppression.Reason.STOP,
            source=Consent.Source.STOP,
        )
        self.assertTrue(
            Suppression.objects.filter(account=self.account, channel=CHANNEL_SMS).exists()
        )


# ---------------------------------------------------------------------------
# Ticken (sending/tick.py)
# ---------------------------------------------------------------------------


@LIVE
class TickTests(EngineFixture, TestCase):
    def test_a_tick_freezes_sends_finishes_and_beats(self):
        self.people(2)
        u = self.utskick(status=Utskick.Status.SCHEDULED, scheduled_at=NOW - timedelta(minutes=1))
        self.assertTrue(tick.work_exists(NOW))
        with mock.patch("apps.utskick.sending.sms.time.sleep"):
            summary = tick.run(NOW, budget=20)
        self.assertEqual(summary["status"], "worked", summary)
        self.assertEqual(summary["start"], {"started": 1})
        self.assertEqual(summary["sms"]["sent"], 2)
        self.assertEqual(summary["finish"], {"sent": 1})
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.SENT)
        self.assertTrue(u.stats)  # reports.final_stats (eller grundsiffrorna)
        row = Switchboard.get_solo()
        self.assertEqual(row.last_tick_summary["sms"]["sent"], 2)
        line = tick.summary_line(summary)
        self.assertIn("sms sent=2", line)
        self.assertNotIn("+4670", line)
        self.assertNotIn("+4670", str(row.last_tick_summary))
        self.assertFalse(tick.work_exists(NOW))

    def test_the_tick_pauses_utskick_of_accounts_that_cannot_send(self):
        u = self.sending(1)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=False)
        self.assertTrue(tick.work_exists(NOW))
        tick.run(NOW, budget=20)
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), ("paused", "account_disabled"))
        self.assertEqual(self.fake.sends, [])

    def test_stops_over_two_percent_pause_and_alert(self):
        u = self.sending(4)
        Recipient.objects.filter(utskick=u).update(status=Recipient.Status.DELIVERED)
        Recipient.objects.filter(pk=u.recipients.first().pk).update(stopped_at=NOW)
        with mock.patch.object(loop, "STOPS_MIN_DELIVERED", 3):
            self.assertEqual(loop.stops_check(NOW), {"paused": 1})
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), ("paused_health", "stops"))
        self.assertIn("1 av 4", u.stats["pause"]["note"])
        self.assertEqual(len(mail.outbox), 1)

    def test_the_command_takes_only(self):
        from io import StringIO

        from django.core.management import call_command

        u = self.sending(1)
        out = StringIO()
        with mock.patch.object(tick, "run", return_value={"status": "idle"}) as run:
            call_command("utskick_tick", "--only", str(u.pk), stdout=out)
        self.assertEqual(run.call_args.kwargs["only"], u.pk)


# ---------------------------------------------------------------------------
# Låsen (select_for_update med skip_locked)
# ---------------------------------------------------------------------------


@LIVE
class ClaimLockTests(TransactionTestCase):
    def setUp(self):
        keys.forget_verified()
        customer = Customer.objects.create(name="Exempelrör AB")
        self.account = FlamingoAccount.objects.create(customer=customer, is_enabled=True)
        enable_utskick(self.account, "exempelror", "Exempelrör")
        self.utskick = Utskick.objects.create(
            account=self.account, name="Låset", status=Utskick.Status.SENDING
        )
        self.rows = [
            Recipient.objects.create(
                utskick=self.utskick, channel=CHANNEL_SMS, address=phone(n), status="queued"
            )
            for n in range(1, 5)
        ]

    def test_a_locked_recipient_is_skipped_not_waited_for(self):
        other = connections.create_connection("default")
        try:
            other.set_autocommit(False)
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM utskick_recipient WHERE id IN (%s, %s) FOR UPDATE",
                    [self.rows[0].pk, self.rows[1].pk],
                )
            claimed = loop.claim(self.account, 10, NOW)
            self.assertEqual({r.pk for r in claimed}, {self.rows[2].pk, self.rows[3].pk})
        finally:
            other.rollback()
            other.close()
        self.assertEqual(Recipient.objects.filter(status=Recipient.Status.SENDING).count(), 2)


# ---------------------------------------------------------------------------
# Retentionen (retention.purge_s2, E.7)
# ---------------------------------------------------------------------------


@LIVE
class RetentionTests(EngineFixture, TestCase):
    def test_s2_rows_follow_e7_and_the_stats_stay(self):
        from . import retention
        from .models import Click, InboundMessage

        now = timezone.now()
        old_u = self.sending(
            2, sms_sender_kind=Utskick.SenderKind.NAME, sms_sender_name="Exempelror"
        )
        self.run_sms()
        tick.finish(NOW)
        Utskick.objects.filter(pk=old_u.pk).update(finished_at=now - timedelta(days=430))
        new_u = self.freeze(self.utskick())
        recipient = new_u.recipients.first()
        Click.objects.create(
            account=self.account, recipient=recipient, at=now - timedelta(days=430)
        )
        Click.objects.create(account=self.account, recipient=recipient, at=now)
        Click.objects.create(
            account=self.account,
            recipient=recipient,
            kind=Click.Kind.SCANNER,
            at=now - timedelta(days=15),
        )
        confirm = codes.create_confirm(self.account, value_hash="x", purpose=LinkCode.Purpose.START)
        LinkCode.objects.filter(pk=confirm.pk).update(created_at=now - timedelta(days=8))
        person = LinkCode.objects.filter(kind=LinkCode.Kind.PERSON).first()
        LinkCode.objects.filter(pk=person.pk).update(created_at=now - timedelta(days=37 * 31))
        InboundMessage.objects.create(
            channel=CHANNEL_SMS, provider_id="s1", received_at=now - timedelta(days=91)
        )
        InboundMessage.objects.create(channel=CHANNEL_SMS, provider_id="s2", received_at=now)

        # Sena leveransrapporter efter att utskicket blev klart: stats räknas
        # om ur mottagarna precis innan de tas bort.
        Utskick.objects.filter(pk=old_u.pk).update(stats={"delivered": 0, "pause": {"note": "x"}})
        old_u.recipients.update(status=Recipient.Status.DELIVERED, delivered_at=now)

        counts = retention.purge_s2(now)

        self.assertEqual(counts["stats"], 1)
        old_u.refresh_from_db()
        self.assertEqual(old_u.stats["delivered"], 2)
        self.assertEqual(old_u.stats["pause"], {"note": "x"})
        self.assertTrue(reports.summary(old_u)["from_stats"])
        self.assertEqual(reports.summary(old_u)["delivered"], 2)
        self.assertEqual(counts["recipients"], 2)
        self.assertEqual(counts["clicks"], 2)
        self.assertEqual(counts["inbound"], 1)
        self.assertFalse(old_u.recipients.exists())
        self.assertEqual(new_u.recipients.count(), 2)
        old_u.refresh_from_db()
        self.assertTrue(old_u.stats)
        self.assertFalse(LinkCode.objects.filter(link__utskick=old_u).exists())
        self.assertTrue(LinkCode.objects.filter(link__utskick=new_u).exists())
        self.assertFalse(LinkCode.objects.filter(pk__in=[confirm.pk, person.pk]).exists())
        # Den andra personkoden finns kvar men har tappat mottagaren.
        self.assertTrue(
            LinkCode.objects.filter(kind=LinkCode.Kind.PERSON, recipient__isnull=True).exists()
        )
        self.assertEqual(Click.objects.count(), 1)
        self.assertIn("s2", retention.daily(now))

    def test_month_end_lists_utskick_sms_waiting_for_a_check(self):
        first = datetime(2026, 11, 1, 2, 45, tzinfo=STOCKHOLM)
        october = datetime(2026, 10, 20, 12, 0, tzinfo=STOCKHOLM)
        for source in ("utskick", "api"):
            SmsMessage.objects.create(
                account=self.sms_account,
                source=source,
                status=SmsMessage.Status.RESERVED,
                to=phone(1),
                body="Hej",
                created_at=october,
            )
        self.assertEqual(recover.month_end_check(first), 1)
        self.assertEqual(recover.month_end_check(first + timedelta(days=1)), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("+4670", mail.outbox[0].body)


# ---------------------------------------------------------------------------
# Byråns del (manage_sending.py)
# ---------------------------------------------------------------------------


@LIVE
class ManageSendingTests(EngineFixture, TestCase):
    def staff_client(self):
        from django.test import Client

        client = Client()
        client.force_login(self.staff)
        return client

    def test_the_overview_shows_the_queue_the_breaker_and_the_probe(self):
        u = self.sending(2)
        state.pause(u, Utskick.PauseReason.STOPS, note="2 av 90 mottagare har avregistrerat sig.")
        Switchboard.objects.update(sms_paused_until=timezone.now() + timedelta(minutes=5))
        response = self.staff_client().get("/manage/utskick/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nödbromsen är dragen.")
        self.assertContains(response, "Höstservice värmepump")
        self.assertContains(response, "Pausat: avregistreringar")
        self.assertContains(response, "Provsms till mig")
        self.assertContains(response, "Exempelrör AB")
        self.assertNotContains(response, "+4670")

    def test_an_information_override_needs_a_reason_and_is_logged(self):
        u = self.utskick(
            status=Utskick.Status.DRAFT,
            purpose=INFORMATION,
            body="Nu 20 % rabatt",
            info_reason="annat",
            info_reason_text="Prisändring",
        )
        url = f"/manage/utskick/utskick/{u.pk}/undantag/"
        self.staff_client().post(url, {"reason": ""})
        u.refresh_from_db()
        self.assertEqual(u.content_override, {})
        with self.assertLogs("apps.utskick.manage_sending", level="WARNING"):
            self.staff_client().post(url, {"reason": "Lagstadgad prisinformation"})
        u.refresh_from_db()
        self.assertEqual(u.content_override["reason"], "Lagstadgad prisinformation")
        self.assertEqual(u.content_override["user"], self.staff.pk)
        self.assertEqual(checks.information_problems(u), [])
        # Säkerhetsgranskningen S2: undantaget följde med en senare ändring av
        # texten. Nu gäller det bara texten, reservtexterna och skälet det gavs för.
        for change in (
            {"sms_body": "50 % rabatt på allt hos Exempelrör med koden VINTER."},
            {"merge_fallbacks": {"förnamn": "halva priset"}},
            {"info_reason_text": "Något annat"},
        ):
            with self.subTest(change=change):
                changed = Utskick.objects.get(pk=u.pk)
                for name, value in change.items():
                    setattr(changed, name, value)
                self.assertIn(checks.LOOKS_LIKE_AD_TEXT, checks.information_problems(changed))
        Utskick.objects.filter(pk=u.pk).update(sms_body="Nu 25 % rabatt")
        html = self.staff_client().get("/manage/utskick/").content.decode()
        self.assertIn("Texten har ändrats sedan dess, så undantaget gäller inte.", html)
        self.staff_client().post(url, {"action": "remove"})
        u.refresh_from_db()
        self.assertEqual(u.content_override, {})
        reklam = self.utskick(status=Utskick.Status.DRAFT)
        self.staff_client().post(f"/manage/utskick/utskick/{reklam.pk}/undantag/", {"reason": "x"})
        reklam.refresh_from_db()
        self.assertEqual(reklam.content_override, {})
        self.assertEqual(mail.outbox, [])

    def test_the_customer_cannot_reach_the_agency_views(self):
        u = self.utskick(status=Utskick.Status.DRAFT, purpose=INFORMATION)
        client = self.client_for(self.anna)
        response = client.post(f"/manage/utskick/utskick/{u.pk}/undantag/", {"reason": "x"})
        self.assertNotEqual(response.status_code, 200)
        u.refresh_from_db()
        self.assertEqual(u.content_override, {})

    def test_the_probe_sends_from_the_reply_number_before_sms_is_on(self):
        Switchboard.objects.update(sms_enabled=False)
        response = self.staff_client().post(
            "/manage/utskick/prov/", {"to": "070-174 06 01", "customer": self.customer.pk}
        )
        self.assertEqual(response.status_code, 302)
        msg = SmsMessage.objects.get()
        self.assertEqual((msg.source, msg.sender, msg.to), ("test", REPLY, "+46701740601"))
        self.assertIn("Exempelrör", msg.body)
        self.assertEqual(self.fake.sends[0]["from"], REPLY)

    def test_the_probe_refuses_bad_input_the_demo_and_the_breaker(self):
        client = self.staff_client()
        client.post("/manage/utskick/prov/", {"to": "hej", "customer": self.customer.pk})
        client.post("/manage/utskick/prov/", {"to": phone(1), "customer": self.other_customer.pk})
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        client.post("/manage/utskick/prov/", {"to": phone(1), "customer": self.customer.pk})
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=False)
        Switchboard.objects.update(sms_paused_until=timezone.now() + timedelta(minutes=5))
        client.post("/manage/utskick/prov/", {"to": phone(1), "customer": self.customer.pk})
        self.assertFalse(SmsMessage.objects.exists())
        self.assertEqual(self.fake.calls, [])

    def test_the_probe_is_limited_per_hour(self):
        client = self.staff_client()
        with mock.patch("apps.utskick.manage_sending.PROBES_PER_HOUR", 1):
            for _ in range(3):
                client.post("/manage/utskick/prov/", {"to": phone(1), "customer": self.customer.pk})
        self.assertEqual(SmsMessage.objects.count(), 1)


# ---------------------------------------------------------------------------
# Granskningarna av S2 (2026-10-10): länkarna efter bekräftelsen, referenserna,
# återhämtningen, leveransrapporter i glappet, priset per land, stats
# ---------------------------------------------------------------------------


@LIVE
class ReviewFindingTests(EngineFixture, TestCase):
    def refuse(self, host):
        AllowedHost.objects.update_or_create(
            account=self.account, host=host, defaults={"status": AllowedHost.Status.REFUSED}
        )

    def test_a_link_adx_refused_after_the_confirmation_stops_the_send(self):
        """Säkerhetsgranskningen S2: värdens läge prövades bara i Granska, så
        ett schemalagt utskick skickades med en nekad länk."""
        self.people(3)
        u = self.utskick(status=Utskick.Status.SCHEDULED)
        TrackedLink.objects.filter(utskick=u).update(
            destination="https://phish-login.example/konto"
        )
        self.refuse("phish-login.example")
        counts = freeze.start_due(NOW)
        self.assertEqual(counts["content"], 1)
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), (Utskick.Status.PAUSED, "content"))
        self.assertEqual(self.fake.sends, [])
        # Efter frysningen: förkontrollerna prövar igen.
        later = self.utskick()
        self.freeze(later)
        self.assertEqual(later.status, Utskick.Status.SENDING)
        TrackedLink.objects.filter(utskick=later).update(
            destination="https://phish-login.example/konto"
        )
        verdict = state.prechecks(later, NOW)
        self.assertEqual((verdict.state, verdict.reason), (Utskick.Status.PAUSED, "content"))
        self.assertEqual(verdict.text, "ADX har inte godkänt länkar till phish-login.example.")
        # En väntande värd likadant, och en ny bekräftelse går inte förbi.
        AllowedHost.objects.filter(host="phish-login.example").update(
            status=AllowedHost.Status.PENDING
        )
        self.assertEqual(state.prechecks(later, NOW).reason, "content")
        self.assertTrue(state.can_reopen(u))

    def test_the_whole_tick_sends_nothing_with_a_refused_link(self):
        self.people(2)
        u = self.utskick(status=Utskick.Status.SCHEDULED)
        TrackedLink.objects.filter(utskick=u).update(destination="https://phish-login.example/x")
        self.refuse("phish-login.example")
        with mock.patch("apps.utskick.sending.sms.time.sleep"):
            tick.run(NOW, budget=20)
        u.refresh_from_db()
        self.assertEqual(u.pause_reason, "content")
        self.assertEqual(self.fake.sends, [])

    def test_an_api_key_cannot_take_over_a_recipients_reference(self):
        """Säkerhetsgranskningen S2: API:t kunde skicka med referensen
        u<utskick>:<mottagare>, och slingan tog då över API:ts sms."""
        u = self.sending(1)
        r = u.recipients.get()
        api = SmsMessage.objects.create(
            account=self.sms_account,
            source="api",
            to=phone(9999),
            sender="Exempelror",
            body="Från API:t",
            reference=f"u{u.pk}:{r.pk}",
            status=SmsMessage.Status.SENT,
            parts=1,
        )
        self.run_sms()
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.SENT)
        self.assertNotEqual(r.sms_message_id, api.pk)
        self.assertEqual(r.sms_message.reference, f"~u{u.pk}:{r.pk}")
        self.assertEqual(len(self.fake.sends), 1)
        # Referenser med ~ går inte att använda från API:t.
        out = service.send_for_account(
            self.sms_account,
            {"to": phone(9998), "message": "Hej", "reference": f"~u{u.pk}:{r.pk}"},
            source="api",
        )
        self.assertEqual(out.error, "invalid_request")
        out = service.send_for_account(
            self.sms_account,
            {"to": phone(9998), "message": "Hej"},
            source="api",
            internal_reference="~x1",
        )
        self.assertEqual(out.error, "invalid_request")

    def test_a_conflicting_message_is_adopted_only_when_it_is_the_recipients_own(self):
        u = self.sending(1)
        r = u.recipients.get()
        Recipient.objects.filter(pk=r.pk).update(status=Recipient.Status.SENDING)
        r.refresh_from_db()
        foreign = SmsMessage.objects.create(
            account=self.sms_account,
            source="utskick",
            to=phone(9999),
            sender=REPLY,
            body="Hej",
            reference=f"~u{u.pk}:{r.pk}",
            status=SmsMessage.Status.SENT,
            parts=1,
        )
        out = service.fail("reference_conflict", message=foreign)
        ctx = loop.Context()
        self.assertEqual(loop.apply(r, out, REPLY, self.account, NOW, ctx), "failed")
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.FAILED)
        self.assertIsNone(r.sms_message_id)
        # Recover tar bara mottagarens eget: utskick och mottagarens nummer.
        Recipient.objects.filter(pk=r.pk).update(
            status=Recipient.Status.SENDING, claimed_at=timezone.now() - timedelta(minutes=10)
        )
        self.assertIsNone(recover.held_message(r))
        SmsMessage.objects.filter(pk=foreign.pk).update(to=r.address)
        self.assertEqual(recover.held_message(r).pk, foreign.pk)
        SmsMessage.objects.filter(pk=foreign.pk).update(source="api")
        self.assertIsNone(recover.held_message(r))

    def test_recovery_does_not_requeue_into_a_cancelled_utskick(self):
        u = self.sending(1)
        r = u.recipients.get()
        Recipient.objects.filter(pk=r.pk).update(
            status=Recipient.Status.SENDING,
            claimed_at=timezone.now() - timedelta(minutes=10),
            attempts=1,
        )
        state.pause(u, Utskick.PauseReason.CUSTOMER, actor=CUSTOMER)
        self.assertTrue(state.cancel(u, actor=CUSTOMER))
        self.assertEqual(recover.recover_stale(), {"requeued": 1})
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.CANCELLED)

    def test_a_delivery_report_in_the_gap_is_not_lost(self):
        """Sändningsgranskningen: rapporten som kom mellan 46elks svar och
        kopplingen till mottagaren gick förlorad."""
        u = self.sending(1)
        r = u.recipients.get()
        Recipient.objects.filter(pk=r.pk).update(status=Recipient.Status.SENDING)
        message = SmsMessage.objects.create(
            account=self.sms_account,
            source="utskick",
            to=r.address,
            sender=REPLY,
            body="Hej",
            status=SmsMessage.Status.SENT,
            parts=1,
        )
        # Rapporten hinner före: raden i databasen är levererad, objektet inte.
        SmsMessage.objects.filter(pk=message.pk).update(
            status=SmsMessage.Status.DELIVERED, delivered_at=timezone.now()
        )
        self.assertEqual(loop.adopt(r, message, REPLY), Recipient.Status.SENT)
        r.refresh_from_db()
        self.assertEqual(r.status, Recipient.Status.DELIVERED)

    def test_the_price_hint_is_per_country_and_only_from_a_dryrun(self):
        ctx = loop.Context()
        se = SmsMessage(country="SE", parts=2, estimated_cost=10400)
        with mock.patch.object(loop.pricing, "recent_part_cost", return_value=None):
            loop._remember_price(ctx, se)
        self.assertEqual(ctx.hints, {"SE": 5200})
        no = SmsMessage(country="NO", parts=1, estimated_cost=9000)
        with mock.patch.object(loop.pricing, "recent_part_cost", return_value=6000):
            loop._remember_price(ctx, no)
        # Norge har historik: ingen gissning därifrån, och svenska priset
        # används aldrig för ett norskt nummer.
        self.assertEqual(ctx.hints, {"SE": 5200})
        self.assertIsNone(ctx.hints.get(loop._country("+4741234567")))
        self.assertEqual(ctx.hints.get(loop._country(phone(1))), 5200)

    def test_stats_are_refreshed_daily_for_thirty_days(self):
        from . import retention

        u = self.sending(2)
        self.run_sms()
        tick.finish(NOW)
        u.refresh_from_db()
        self.assertEqual(u.stats["delivered"], 0)
        u.recipients.update(status=Recipient.Status.DELIVERED, delivered_at=timezone.now())
        Utskick.objects.filter(pk=u.pk).update(finished_at=timezone.now() - timedelta(days=3))
        self.assertEqual(retention.refresh_stats(timezone.now()), 1)
        u.refresh_from_db()
        self.assertEqual(u.stats["delivered"], 2)
        Utskick.objects.filter(pk=u.pk).update(finished_at=timezone.now() - timedelta(days=40))
        self.assertEqual(retention.refresh_stats(timezone.now()), 0)
