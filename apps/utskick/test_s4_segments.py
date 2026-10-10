"""Segmenten (README B.4, I.11, H.1, J S4 test_s4_segments): kompilatorn
per fält och operator, grupperna (ELLER), månader över månadsskiften,
"öppnade" som låst, räkningen per kanal, att räkningen stämmer med
frysningen, främmande id:n (400) och manipulerade regler (inga främmande
kontakter), acceptansens segment "Service i höst", uppföljningen från
rapporten, vyerna (byggaren med och utan skript, räkningen, Listor,
Kontakters filter, kortets chips) och Mottagare i utskicket.

    CompilerTests       varje fält och operator, grupperna, tomt och trasigt
    CleanTests          prövningen: kanonisk form, främmande id:n, fel per regel,
                        gränserna, "öppnade" låst
    DescribeTests       reglerna i klartext
    CountTests          räkningen per kanal, frysningen, undantagen, chipsen
    FollowUpTests       "Följ upp de som inte klickade"
    BuilderViewTests    segment_new, segment_detail och segment_count
    ForeignIdTests      varje väg med ett främmande id ger 400
    IntegrationTests    Listor, Kontakters filter, kortet och Mottagare
    GuardTests          375 px, skriptets kontrakt och texterna

Inget når nätet: EngineFixture (test_s2_tick) lägger apps.sms.elks._post på
en låtsas-46elks. Tiden är fast (NOW, en tisdag 10.00 i Stockholm) där
reglerna räknar dagar och månader.
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from apps.flamingo.models import Lead
from apps.sms.pricing import STOCKHOLM

from . import audience, segments
from . import consent as consents
from .access import ForeignIds
from .app_views import segments as views
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Click,
    Consent,
    Contact,
    ContactList,
    Counter,
    Event,
    FieldDef,
    Recipient,
    Segment,
    Tag,
    Thread,
    ThreadMessage,
    Utskick,
    UtskickSettings,
)
from .segments import SegmentError
from .test_s2_tick import NOW, EngineFixture
from .testing import make_contact

BASE = Path(settings.BASE_DIR)


class SegmentFixture(EngineFixture):
    """EngineFixture (Kunder, sms-kontot, Switchboard på) och ett datumfält,
    ett textfält, ett talfält och ett valfält hos Exempelrör."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.service = FieldDef.objects.create(
            account=cls.account, key="senaste-service", label="Senaste service", kind="date"
        )
        cls.ort = FieldDef.objects.create(account=cls.account, key="ort", label="Ort", kind="text")
        cls.bilar = FieldDef.objects.create(
            account=cls.account, key="bilar", label="Antal bilar", kind="number"
        )
        cls.bransle = FieldDef.objects.create(
            account=cls.account,
            key="bransle",
            label="Bränsle",
            kind="choice",
            choices=["Diesel", "El", "Bensin"],
        )
        cls.vip = Tag.objects.create(account=cls.account, name="VIP")
        cls.bromma = ContactList.objects.create(account=cls.account, name="Bromma")

    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)

    def match(self, *rules, now=NOW):
        """Kontakterna (pk) som reglerna (en lista under "all") ger."""
        found = segments.contacts(self.account, {"all": list(rules)}, now)
        return set(found.values_list("pk", flat=True))

    def with_fields(self, kontakt, **values):
        Contact.objects.filter(pk=kontakt.pk).update(fields=values)
        kontakt.refresh_from_db()
        return kontakt

    def sent(self, name="Höstservice", account=None, **kwargs):
        return Utskick.objects.create(
            account=account or self.account,
            name=name,
            status=Utskick.Status.SENT,
            frozen_at=NOW - timedelta(days=10),
            **kwargs,
        )

    def recipient(self, utskick, kontakt, status=Recipient.Status.DELIVERED, **kwargs):
        kwargs.setdefault("sent_at", NOW - timedelta(days=10))
        return Recipient.objects.create(
            utskick=utskick,
            contact=kontakt,
            channel=CHANNEL_SMS,
            address=kontakt.phone,
            status=status,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Kompilatorn
# ---------------------------------------------------------------------------


class CompilerTests(SegmentFixture, TestCase):
    def test_list_and_tag_in_and_not_in(self):
        a, b, c = self.people(3)
        self.bromma.memberships.create(contact=a)
        self.vip.contacts.add(b)
        self.assertEqual(self.match({"f": "list", "op": "in", "v": [self.bromma.pk]}), {a.pk})
        self.assertEqual(
            self.match({"f": "list", "op": "not_in", "v": [self.bromma.pk]}), {b.pk, c.pk}
        )
        self.assertEqual(self.match({"f": "tag", "op": "in", "v": [self.vip.pk]}), {b.pk})
        self.assertEqual(
            self.match({"f": "tag", "op": "not_in", "v": [str(self.vip.pk)]}), {a.pk, c.pk}
        )

    def test_text_field(self):
        a, b, c = self.people(3)
        self.with_fields(a, ort="Bromma")
        self.with_fields(b, ort="Solna")
        self.assertEqual(self.match({"f": "field:ort", "op": "eq", "v": "bromma"}), {a.pk})
        # "är inte" tar med den som saknar värdet (aldrig NULL).
        self.assertEqual(
            self.match({"f": "field:ort", "op": "not_eq", "v": "Bromma"}), {b.pk, c.pk}
        )
        self.assertEqual(self.match({"f": "field:ort", "op": "contains", "v": "ROM"}), {a.pk})
        self.assertEqual(self.match({"f": "field:ort", "op": "empty"}), {c.pk})
        self.assertEqual(self.match({"f": "field:ort", "op": "not_empty"}), {a.pk, b.pk})

    def test_number_field(self):
        a, b, c, d = self.people(4)
        self.with_fields(a, bilar="1")
        self.with_fields(b, bilar="2.5")
        self.with_fields(c, bilar="inte ett tal")
        self.assertEqual(self.match({"f": "field:bilar", "op": "eq", "v": 1}), {a.pk})
        self.assertEqual(self.match({"f": "field:bilar", "op": "gt", "v": "2"}), {b.pk})
        self.assertEqual(self.match({"f": "field:bilar", "op": "lt", "v": 3}), {a.pk, b.pk})
        self.assertEqual(self.match({"f": "field:bilar", "op": "empty"}), {d.pk})

    def test_choice_field(self):
        a, b, c = self.people(3)
        self.with_fields(a, bransle="Diesel")
        self.with_fields(b, bransle="El")
        self.assertEqual(self.match({"f": "field:bransle", "op": "in", "v": ["El"]}), {b.pk})
        self.assertEqual(
            self.match({"f": "field:bransle", "op": "not_in", "v": ["El"]}), {a.pk, c.pk}
        )

    def test_date_field_in_days_and_months(self):
        # NOW är 13 okt 2026: fem månader bakåt är 13 maj.
        older, edge, recent, future, empty, broken = self.people(6)
        self.with_fields(older, **{"senaste-service": "2026-05-12"})
        self.with_fields(edge, **{"senaste-service": "2026-05-13"})
        self.with_fields(recent, **{"senaste-service": "2026-10-01"})
        self.with_fields(future, **{"senaste-service": "2026-10-20"})
        self.with_fields(broken, **{"senaste-service": "2026-02-31x"})
        field = "field:senaste-service"
        self.assertEqual(self.match({"f": field, "op": "before_months", "v": 5}), {older.pk})
        self.assertEqual(
            self.match({"f": field, "op": "within_months", "v": 5}), {edge.pk, recent.pk}
        )
        self.assertEqual(self.match({"f": field, "op": "within_days", "v": 14}), {recent.pk})
        self.assertEqual(
            self.match({"f": field, "op": "before_days", "v": 12}), {older.pk, edge.pk}
        )
        self.assertEqual(self.match({"f": field, "op": "next_days", "v": 7}), {future.pk})
        self.assertEqual(self.match({"f": field, "op": "next_months", "v": 1}), {future.pk})
        self.assertEqual(self.match({"f": field, "op": "empty"}), {empty.pk})

    def test_months_across_month_ends(self):
        shift = segments.shift_months
        self.assertEqual(shift(date(2026, 3, 31), -1), date(2026, 2, 28))
        self.assertEqual(shift(date(2024, 3, 31), -1), date(2024, 2, 29))
        self.assertEqual(shift(date(2026, 12, 31), 2), date(2027, 2, 28))
        self.assertEqual(shift(date(2026, 1, 15), -13), date(2024, 12, 15))
        a, b = self.people(2)
        self.with_fields(a, **{"senaste-service": "2026-02-27"})
        self.with_fields(b, **{"senaste-service": "2026-02-28"})
        end_of_march = datetime(2026, 3, 31, 12, 0, tzinfo=STOCKHOLM)
        rule = {"f": "field:senaste-service", "op": "before_months", "v": 1}
        self.assertEqual(self.match(rule, now=end_of_march), {a.pk})
        # 00.30 den 1 april i Stockholm är redan april (dagen räknas i
        # Stockholm, inte i UTC där det fortfarande är 31 mars).
        early_april = datetime(2026, 4, 1, 0, 30, tzinfo=STOCKHOLM)
        self.assertEqual(self.match(rule, now=early_april), {a.pk, b.pk})

    def test_the_contacts_own_fields(self):
        a = self.person(first_name="Anna", email="anna@foretag.example")
        b = self.person(first_name="", last_name="Berg", email="")
        self.assertEqual(
            self.match({"f": "contact:email", "op": "contains", "v": "@FORETAG"}), {a.pk}
        )
        self.assertEqual(self.match({"f": "contact:first_name", "op": "empty"}), {b.pk})
        self.assertEqual(self.match({"f": "contact:first_name", "op": "eq", "v": "anna"}), {a.pk})
        self.assertEqual(self.match({"f": "contact:email", "op": "not_empty"}), {a.pk})
        self.assertEqual(self.match({"f": "contact:phone", "op": "not_empty"}), {a.pk, b.pk})
        # Bara fälten i CONTACT_FIELDS: en annan kolumn ger ingen.
        self.assertEqual(self.match({"f": "contact:search_text", "op": "not_empty"}), set())

    def test_consent_can_get_offers_and_status(self):
        yes = self.person()
        declined = self.person(status=consents.DECLINED)
        missing = self.person(status=consents.MISSING)
        sms = {"channel": CHANNEL_SMS}
        self.assertEqual(self.match({"f": "consent", "op": "eligible", "v": sms}), {yes.pk})
        self.assertEqual(
            self.match({"f": "consent", "op": "not_eligible", "v": sms}),
            {declined.pk, missing.pk},
        )
        self.assertEqual(
            self.match(
                {"f": "consent", "op": "in", "v": {"channel": "sms", "status": ["declined"]}}
            ),
            {declined.pk},
        )
        self.assertEqual(
            self.match(
                {"f": "consent", "op": "not_in", "v": {"channel": "sms", "status": ["yes"]}}
            ),
            {declined.pk, missing.pk},
        )

    def test_kind_source_and_created(self):
        person = self.person()
        company = make_contact(self.account, kind="company", company_name="Exempel AB")
        imported = self.person()
        Contact.objects.filter(pk=imported.pk).update(
            source=Contact.Source.IMPORT, created_at=NOW - timedelta(days=60)
        )
        Contact.objects.filter(pk__in=[person.pk, company.pk]).update(
            created_at=NOW - timedelta(days=2)
        )
        self.assertEqual(self.match({"f": "kind", "op": "eq", "v": "company"}), {company.pk})
        self.assertEqual(self.match({"f": "source", "op": "in", "v": ["import"]}), {imported.pk})
        self.assertEqual(
            self.match({"f": "source", "op": "not_in", "v": ["import"]}), {person.pk, company.pk}
        )
        self.assertEqual(self.match({"f": "created", "op": "before_days", "v": 30}), {imported.pk})
        self.assertEqual(
            self.match({"f": "created", "op": "within_months", "v": 1}), {person.pk, company.pk}
        )

    def test_got_opened_and_clicked(self):
        a, b, c = self.people(3)
        u = self.sent()
        older = self.sent("Våren")
        self.recipient(u, a, first_clicked_at=NOW - timedelta(days=9), opened_at=NOW)
        self.recipient(u, b)
        self.recipient(u, c, status=Recipient.Status.SKIPPED, sent_at=None)
        self.recipient(older, c, sent_at=NOW - timedelta(days=200))
        self.assertEqual(self.match({"f": "got_utskick", "op": "in", "v": [u.pk]}), {a.pk, b.pk})
        self.assertEqual(self.match({"f": "got_utskick", "op": "not_in", "v": [u.pk]}), {c.pk})
        self.assertEqual(
            self.match({"f": "got_utskick", "op": "within_days", "v": 30}), {a.pk, b.pk}
        )
        self.assertEqual(self.match({"f": "got_utskick", "op": "not_within_days", "v": 30}), {c.pk})
        self.assertEqual(self.match({"f": "opened", "op": "in", "v": [u.pk]}), {a.pk})
        self.assertEqual(self.match({"f": "clicked", "op": "in", "v": [u.pk]}), {a.pk})
        self.assertEqual(self.match({"f": "clicked", "op": "not_in", "v": [u.pk]}), {b.pk, c.pk})
        Click.objects.create(
            account=self.account, contact=b, channel="sms", kind="human", at=NOW - timedelta(days=1)
        )
        Click.objects.create(account=self.account, contact=c, channel="sms", kind="scanner", at=NOW)
        self.assertEqual(self.match({"f": "clicked", "op": "within_days", "v": 7}), {b.pk})
        self.assertEqual(
            self.match({"f": "clicked", "op": "not_within_days", "v": 7}), {a.pk, c.pk}
        )

    def test_visits_leads_and_replies(self):
        a, b, c = self.people(3)
        for kind, who in ((Event.LP_VISIT, a), (Event.SITE_VISIT, b)):
            Event.objects.create(
                account=self.account, contact=who, kind=kind, at=NOW - timedelta(days=3)
            )
        self.assertEqual(self.match({"f": "visited_lp", "op": "within_days", "v": 7}), {a.pk, b.pk})
        Lead.objects.create(account=self.account, contact=a, created_at=NOW - timedelta(days=5))
        Lead.objects.create(
            account=self.account,
            contact=b,
            source=Lead.SOURCE_REPLY,
            created_at=NOW - timedelta(days=5),
        )
        Lead.objects.create(
            account=self.account,
            contact=c,
            status=Lead.STATUS_JUNK,
            created_at=NOW - timedelta(days=5),
        )
        self.assertEqual(self.match({"f": "lead", "op": "within_days", "v": 30}), {a.pk})
        self.assertEqual(self.match({"f": "lead", "op": "not_within_days", "v": 30}), {b.pk, c.pk})
        for who, kind in ((a, Thread.Kind.REPLY), (c, Thread.Kind.STOP)):
            thread = Thread.objects.create(
                account=self.account, contact=who, channel="sms", kind=kind, address=who.phone
            )
            ThreadMessage.objects.create(
                thread=thread, direction="in", body="Ja tack", at=NOW - timedelta(days=1)
            )
        self.assertEqual(self.match({"f": "replied", "op": "within_days", "v": 7}), {a.pk})

    def test_a_group_is_or_and_the_top_is_and(self):
        a, b, c = self.people(3)
        self.vip.contacts.add(a)
        self.bromma.memberships.create(contact=b)
        rules = [
            {"f": "list", "op": "in", "v": [self.kunder.pk]},
            {
                "any": [
                    {"f": "tag", "op": "in", "v": [self.vip.pk]},
                    {"f": "list", "op": "in", "v": [self.bromma.pk]},
                ]
            },
        ]
        self.assertEqual(self.match(*rules), {a.pk, b.pk})
        self.assertEqual(self.match(rules[1], {"f": "tag", "op": "in", "v": [self.vip.pk]}), {a.pk})

    def test_empty_broken_and_oversized_rules_give_nobody(self):
        self.people(2)
        for rules in (
            {},
            {"all": []},
            {"all": "x"},
            {"all": [{"f": "okänt", "op": "in", "v": [1]}]},
            {"all": [{"f": "list", "op": "eq", "v": [self.kunder.pk]}]},
            {"all": [{"f": "list", "op": "in", "v": ["x"]}]},
            {"all": [{"f": "field:finns-inte", "op": "eq", "v": "x"}]},
            {"all": [{"f": "created", "op": "before_days", "v": -3}]},
            {"all": [{"any": []}]},
            {"all": [{"f": "list", "op": "in", "v": [self.kunder.pk]}] * 21},
            {"annat": []},
            ["inte", "en", "ordbok"],
        ):
            with self.subTest(rules=rules):
                self.assertFalse(segments.contacts(self.account, rules, NOW).exists())
                self.assertEqual(segments.count(self.account, rules, NOW)["total"], 0)

    def test_tampered_ids_never_reach_another_account(self):
        """H.1: regler som skrivits direkt i databasen med ett annat kontos
        id:n ger inga av deras kontakter (och färre av våra)."""
        mine = self.person()
        theirs_list = ContactList.objects.create(account=self.other_account, name="Deras")
        theirs_tag = Tag.objects.create(account=self.other_account, name="Deras")
        stranger = self.person(account=self.other_account, contact_list=theirs_list)
        theirs_tag.contacts.add(stranger)
        theirs_utskick = self.sent("Deras", account=self.other_account)
        self.recipient(theirs_utskick, stranger)
        segment = Segment.objects.create(
            account=self.account,
            name="Manipulerat",
            rules={
                "all": [
                    {
                        "any": [
                            {"f": "list", "op": "in", "v": [theirs_list.pk]},
                            {"f": "tag", "op": "in", "v": [theirs_tag.pk]},
                            {"f": "got_utskick", "op": "in", "v": [theirs_utskick.pk]},
                            {"f": "clicked", "op": "not_in", "v": [theirs_utskick.pk]},
                        ]
                    }
                ]
            },
        )
        found = set(segments.contacts(self.account, segment.rules, NOW))
        self.assertNotIn(stranger, found)
        self.assertEqual(found, {mine})
        their_segment = Segment.objects.create(
            account=self.other_account,
            name="Alla deras",
            rules={"all": [{"f": "contact:phone", "op": "not_empty"}]},
        )
        q = segments.matches_q(self.account.pk, [segment.pk, their_segment.pk], NOW)
        self.assertEqual(set(Contact.objects.filter(account=self.account).filter(q)), {mine})


# ---------------------------------------------------------------------------
# Prövningen
# ---------------------------------------------------------------------------


class CleanTests(SegmentFixture, TestCase):
    def test_the_canonical_form(self):
        cleaned = segments.clean(
            self.account,
            {
                "all": [
                    {"f": "list", "op": "in", "v": [str(self.kunder.pk), self.kunder.pk]},
                    {"f": "field:senaste-service", "op": "before_months", "v": "5"},
                    {"f": "field:bilar", "op": "gt", "v": "2,5"},
                    {"f": "field:bransle", "op": "in", "v": ["diesel"]},
                    {"f": "field:ort", "op": "empty", "v": "ignoreras"},
                    {
                        "any": [
                            {"f": "consent", "op": "eligible", "v": {"channel": "sms", "x": 1}},
                            {"f": "kind", "op": "eq", "v": "company"},
                        ]
                    },
                ]
            },
        )
        self.assertEqual(
            cleaned,
            {
                "all": [
                    {"f": "list", "op": "in", "v": [self.kunder.pk]},
                    {"f": "field:senaste-service", "op": "before_months", "v": 5},
                    {"f": "field:bilar", "op": "gt", "v": 2.5},
                    {"f": "field:bransle", "op": "in", "v": ["Diesel"]},
                    {"f": "field:ort", "op": "empty"},
                    {
                        "any": [
                            {"f": "consent", "op": "eligible", "v": {"channel": "sms"}},
                            {"f": "kind", "op": "eq", "v": "company"},
                        ]
                    },
                ]
            },
        )
        self.assertEqual(segments.clean(self.account, cleaned), cleaned)
        self.assertEqual(segments.clean(self.account, {}), {"all": []})

    def test_foreign_and_garbage_ids_raise(self):
        theirs_list = ContactList.objects.create(account=self.other_account, name="Deras")
        theirs_tag = Tag.objects.create(account=self.other_account, name="Deras")
        theirs = self.sent("Deras", account=self.other_account)
        for rule in (
            {"f": "list", "op": "in", "v": [theirs_list.pk]},
            {"f": "list", "op": "not_in", "v": [self.kunder.pk, theirs_list.pk]},
            {"f": "tag", "op": "in", "v": [theirs_tag.pk]},
            {"f": "got_utskick", "op": "in", "v": [theirs.pk]},
            {"f": "clicked", "op": "not_in", "v": [theirs.pk]},
            {"f": "list", "op": "in", "v": ["1 OR 1=1"]},
            {"f": "list", "op": "in", "v": [10**9]},
        ):
            with self.subTest(rule=rule), self.assertRaises(ForeignIds):
                segments.clean(self.account, {"all": [rule]})
            # Den levande räkningen nekar likadant.
            with self.subTest(rule=rule, partial=True), self.assertRaises(ForeignIds):
                segments.clean_partial(self.account, {"all": [rule]})

    def test_errors_per_rule(self):
        rules = {
            "all": [
                {"f": "list", "op": "in", "v": [self.kunder.pk]},
                {"f": "list", "op": "in", "v": [""]},
                {"any": [{"f": "field:bilar", "op": "lt", "v": "många"}, {"f": ""}]},
                {"f": "field:borttaget", "op": "eq", "v": "x"},
                {"f": "created", "op": "before_months", "v": 500},
                {"f": "field:bransle", "op": "in", "v": ["Gas"]},
            ]
        }
        with self.assertRaises(SegmentError) as caught:
            segments.clean(self.account, rules)
        rows = caught.exception.rows
        self.assertEqual(rows[2], segments.MISSING_TEXTS["list"])
        self.assertEqual(rows[3], segments.NUMBER_TEXT)
        self.assertEqual(rows[4], segments.MISSING_TEXTS["field"])
        self.assertEqual(rows[5], segments.GONE_FIELD_TEXT)
        self.assertEqual(rows[6], segments.MONTHS_TEXT)
        self.assertEqual(rows[7], segments.CHOICE_TEXT)
        self.assertNotIn(1, rows)
        self.assertIn("Villkor 2: välj en lista.", caught.exception.errors)

    def test_partial_skips_what_is_not_filled_in(self):
        rules, skipped = segments.clean_partial(
            self.account,
            {
                "all": [
                    {"f": "list", "op": "in", "v": [self.kunder.pk]},
                    {"f": "tag", "op": "in", "v": []},
                    {"f": "lead", "op": "not_within_days", "v": ""},
                ]
            },
        )
        self.assertEqual(rules, {"all": [{"f": "list", "op": "in", "v": [self.kunder.pk]}]})
        self.assertEqual(skipped, [2, 3])
        with self.assertRaises(SegmentError):
            segments.clean_partial(
                self.account, {"all": [{"f": "created", "op": "within_days", "v": "x"}]}
            )

    def test_the_limits(self):
        rule = {"f": "list", "op": "in", "v": [self.kunder.pk]}
        with self.assertRaises(SegmentError) as caught:
            segments.clean(self.account, {"all": [rule] * (segments.MAX_RULES + 1)})
        self.assertEqual(caught.exception.errors, [segments.TOO_MANY_RULES_TEXT])
        groups = [{"any": [rule]}] * (segments.MAX_GROUPS + 1)
        with self.assertRaises(SegmentError) as caught:
            segments.clean(self.account, {"all": groups})
        self.assertEqual(caught.exception.errors, [segments.TOO_MANY_GROUPS_TEXT])
        self.assertEqual(
            len(segments.clean(self.account, {"all": [rule] * segments.MAX_RULES})["all"]),
            segments.MAX_RULES,
        )

    def test_opened_is_locked_until_open_tracking_was_on(self):
        u = self.sent()
        rule = {"f": "opened", "op": "in", "v": [u.pk]}
        self.assertTrue(segments.opened_locked(self.account))
        with self.assertRaises(SegmentError) as caught:
            segments.clean(self.account, {"all": [rule]})
        self.assertEqual(caught.exception.rows[1], segments.OPENED_LOCKED_TEXT)
        Utskick.objects.filter(pk=u.pk).update(open_tracking=True)
        self.assertFalse(segments.opened_locked(self.account))
        self.assertEqual(segments.clean(self.account, {"all": [rule]})["all"], [rule])
        Utskick.objects.filter(pk=u.pk).update(open_tracking=False)
        UtskickSettings.objects.filter(account=self.account).update(open_tracking=True)
        self.assertFalse(segments.opened_locked(self.account))
        self.assertEqual(
            segments.OPENED_LOCKED_TEXT,
            "Öppningar spåras inte. Slå på Spåra öppningar under Inställningar.",
        )


# ---------------------------------------------------------------------------
# Klartexten
# ---------------------------------------------------------------------------


class DescribeTests(SegmentFixture, TestCase):
    def test_the_acceptance_segment_reads_as_it_should(self):
        lines = segments.describe(self.account, service_rules(self.kunder))
        self.assertEqual(
            lines,
            [
                "Senaste service äldre än 5 månader",
                "Finns i listan Kunder",
                "Ingen förfrågan de senaste 30 dagarna",
            ],
        )

    def test_groups_names_and_what_is_gone(self):
        u = self.sent("Höstservice")
        gone = ContactList.objects.create(account=self.account, name="Gammal")
        gone_pk = gone.pk
        gone.delete()
        theirs = ContactList.objects.create(account=self.other_account, name="Hemlig")
        lines = segments.describe(
            self.account,
            {
                "all": [
                    {
                        "any": [
                            {"f": "tag", "op": "in", "v": [self.vip.pk]},
                            {"f": "clicked", "op": "not_in", "v": [u.pk]},
                        ]
                    },
                    {"f": "list", "op": "in", "v": [gone_pk]},
                    {"f": "list", "op": "not_in", "v": [theirs.pk]},
                    {"f": "field:bilar", "op": "gt", "v": 2.5},
                    {"f": "consent", "op": "eligible", "v": {"channel": "email"}},
                    {"f": "created", "op": "within_days", "v": 1},
                    {"f": "field:senaste-service", "op": "next_months", "v": 2},
                    {"f": "trasig"},
                ]
            },
        )
        self.assertEqual(
            lines,
            [
                "Minst ett av: Har taggen VIP; Klickade inte i utskicket Höstservice",
                "Finns i en borttagen lista",
                "Finns inte i en borttagen lista",
                "Antal bilar är större än 2,5",
                "Kan få erbjudanden via e-post",
                "Tillagd den senaste dagen",
                "Senaste service inom de kommande 2 månaderna",
                "Ett villkor som inte går att läsa",
            ],
        )
        self.assertNotIn("Hemlig", " ".join(lines))


def service_rules(kunder):
    """Acceptansens segment (J S4): Senaste service äldre än 5 månader OCH
    listan Kunder OCH ingen förfrågan de senaste 30 dagarna."""
    return {
        "all": [
            {"f": "field:senaste-service", "op": "before_months", "v": 5},
            {"f": "list", "op": "in", "v": [kunder.pk]},
            {"f": "lead", "op": "not_within_days", "v": 30},
        ]
    }


# ---------------------------------------------------------------------------
# Räkningen, frysningen och chipsen
# ---------------------------------------------------------------------------


class CountTests(SegmentFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.both = self.person(email="both@kund.example")
        consents.set_status(
            self.both, CHANNEL_EMAIL, consents.YES, source=Consent.Source.MANUAL, evidence="kassan"
        )
        self.sms_only = self.person()
        self.none = self.person(status=consents.MISSING, email="none@kund.example")
        self.declined = self.person(status=consents.DECLINED)
        self.outside = self.person(contact_list=self.bromma)
        self.rules = {"all": [{"f": "list", "op": "in", "v": [self.kunder.pk]}]}

    def test_counts_per_channel(self):
        self.assertEqual(
            segments.count(self.account, self.rules, NOW), {"total": 4, "sms": 2, "email": 1}
        )

    def test_the_count_matches_the_freeze(self):
        segment = Segment.objects.create(account=self.account, name="Kunder", rules=self.rules)
        counted = segments.count(self.account, segment.rules, NOW)
        u = self.utskick(audience={"segments": [segment.pk]})
        u = self.freeze(u)
        queued = u.recipients.filter(status=Recipient.Status.QUEUED)
        self.assertEqual(u.recipients.count(), counted["total"])
        self.assertEqual(queued.filter(channel=CHANNEL_SMS).count(), counted["sms"])
        self.assertEqual(
            set(queued.values_list("contact_id", flat=True)), {self.both.pk, self.sms_only.pk}
        )
        # Granska och Mottagare räknar som frysningen, e-posten också.
        draft = self.utskick(
            status=Utskick.Status.DRAFT,
            audience={"segments": [segment.pk]},
            channel_mode=Utskick.ChannelMode.EMAIL_ONLY,
        )
        numbers = audience.count(draft, NOW)
        self.assertEqual(numbers["total"], counted["total"])
        self.assertEqual(numbers["email"], counted["email"])

    def test_a_segment_and_a_list_together_and_an_excluded_segment(self):
        rich = Segment.objects.create(
            account=self.account,
            name="Har bilar",
            rules={"all": [{"f": "field:bilar", "op": "gt", "v": 1}]},
        )
        self.with_fields(self.both, bilar="2")
        u = self.utskick(
            status=Utskick.Status.DRAFT,
            audience={"lists": [self.bromma.pk], "segments": [rich.pk]},
        )
        self.assertEqual(set(audience.contacts(u, NOW)), {self.outside, self.both})
        # Undantaget tar bort segmentets kontakter och behåller de som
        # saknar fältet (matches_q är pk IN, aldrig NULL).
        u.audience = {"lists": [self.kunder.pk], "exclude": {"segments": [rich.pk]}}
        self.assertEqual(set(audience.contacts(u, NOW)), {self.sms_only, self.none, self.declined})

    def test_a_tampered_segment_id_in_the_audience_freezes_nobody_foreign(self):
        stranger = self.person(account=self.other_account)
        theirs = Segment.objects.create(
            account=self.other_account,
            name="Deras",
            rules={"all": [{"f": "contact:phone", "op": "not_empty"}]},
        )
        u = self.utskick()
        Utskick.objects.filter(pk=u.pk).update(audience={"segments": [theirs.pk, 10**9]})
        u = self.freeze(Utskick.objects.get(pk=u.pk))
        self.assertFalse(u.recipients.exists())
        self.assertFalse(Recipient.objects.filter(contact=stranger).exists())

    def test_refresh_and_the_daily_recount(self):
        segment = Segment.objects.create(account=self.account, name="Kunder", rules=self.rules)
        segments.refresh(segment, NOW)
        segment.refresh_from_db()
        self.assertEqual(
            (segment.cached_count, segment.cached_sms, segment.cached_email), (4, 2, 1)
        )
        self.assertEqual(segment.counted_at, NOW)
        self.person()
        theirs = Segment.objects.create(
            account=self.other_account,
            name="Av",
            rules={"all": [{"f": "kind", "op": "eq", "v": "person"}]},
        )
        UtskickSettings.objects.filter(account=self.other_account).update(is_enabled=False)
        from . import retention

        summary = retention.daily(NOW + timedelta(days=1))
        self.assertEqual(summary["segments"], 1)
        segment.refresh_from_db()
        self.assertEqual(segment.cached_count, 5)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.counted_at)

    def test_the_count_is_one_query(self):
        # Granskningen: tre COUNT räknade om segmentet tre gånger.
        with self.assertNumQueries(1):
            counted = segments.count(self.account, self.rules, NOW)
        self.assertEqual(counted, {"total": 4, "sms": 2, "email": 1})
        nobody = {"all": [{"f": "kind", "op": "eq", "v": "company"}]}
        self.assertEqual(
            segments.count(self.account, nobody, NOW), {"total": 0, "sms": 0, "email": 0}
        )

    def test_can_get_offers_is_a_condition_on_the_contact(self):
        # Granskningen: "kan få erbjudanden" var pk IN (kontots alla kontakter
        # som kan få), som for_contacts CASE räknade om för varje segment.
        rules = {"all": [{"f": "consent", "op": "eligible", "v": {"channel": "sms"}}]}
        sql = str(segments.contacts(self.account, rules, NOW).values("pk").query)
        self.assertNotIn('"id" IN (SELECT', sql)
        self.assertEqual(
            set(segments.contacts(self.account, rules, NOW)),
            set(
                consents.eligible_contacts(
                    Contact.objects.filter(account=self.account), CHANNEL_SMS, consents.REKLAM
                )
            ),
        )
        offers = Segment.objects.create(account=self.account, name="Kan få", rules=rules)
        self.assertEqual(segments.for_contact(self.sms_only, NOW), [offers])
        self.assertEqual(segments.for_contact(self.declined, NOW), [])

    def test_for_contact_lists_the_segments_it_is_in(self):
        a = Segment.objects.create(account=self.account, name="A Kunder", rules=self.rules)
        b = Segment.objects.create(
            account=self.account,
            name="B Bromma",
            rules={"all": [{"f": "list", "op": "in", "v": [self.bromma.pk]}]},
        )
        Segment.objects.create(account=self.account, name="C Trasigt", rules={"all": "x"})
        self.assertEqual(segments.for_contact(self.both, NOW), [a])
        self.assertEqual(segments.for_contact(self.outside, NOW), [b])
        self.assertEqual(segments.for_contact(self.person(account=self.other_account)), [])


# ---------------------------------------------------------------------------
# Uppföljningen från rapporten (I.8)
# ---------------------------------------------------------------------------


class FollowUpTests(SegmentFixture, TestCase):
    def test_the_rules_and_the_segment(self):
        clicker, quiet, skipped = self.people(3)
        u = self.sent("Höstservice värmepump")
        self.recipient(u, clicker, first_clicked_at=NOW)
        self.recipient(u, quiet)
        self.recipient(u, skipped, status=Recipient.Status.SKIPPED, sent_at=None)
        self.assertEqual(
            segments.follow_up_rules(u),
            {
                "all": [
                    {"f": "got_utskick", "op": "in", "v": [u.pk]},
                    {"f": "clicked", "op": "not_in", "v": [u.pk]},
                ]
            },
        )
        first = segments.create_follow_up(u, user=self.anna, now=NOW)
        self.assertEqual(first.name, "Klickade inte: Höstservice värmepump")
        self.assertEqual(first.created_by, self.anna)
        self.assertEqual(set(segments.contacts(self.account, first.rules, NOW)), {quiet})
        self.assertEqual(first.cached_count, 1)
        self.assertEqual(segments.clean(self.account, first.rules), first.rules)
        second = segments.create_follow_up(u, user=None, now=NOW)
        self.assertEqual(second.name, "Klickade inte: Höstservice värmepump 2")
        self.assertIsNone(second.created_by)
        long = self.sent("X" * 120)
        self.assertEqual(len(segments.create_follow_up(long, user=None).name), 80)

    def test_who_reported_it_as_spam_is_never_followed_up(self):
        # Granskningen: klagomålet spärrar bara e-posten, så "Fick X" med
        # SENT_LIKE gav dem uppföljningen med sms.
        quiet, spam = self.people(2)
        u = self.sent("Höstbrevet")
        self.recipient(u, quiet)
        self.recipient(u, spam)
        Recipient.objects.create(
            utskick=u,
            contact=spam,
            channel=CHANNEL_EMAIL,
            address="spam@kund.example",
            status=Recipient.Status.COMPLAINED,
            sent_at=NOW - timedelta(days=10),
        )
        segment = segments.create_follow_up(u, user=self.anna, now=NOW)
        self.assertEqual(set(segments.contacts(self.account, segment.rules, NOW)), {quiet})
        self.assertEqual(segment.cached_count, 1)
        from . import reports

        self.assertEqual(reports.follow_up_count(u), 1)
        # "Fick inte X" tar inte heller med dem: de fick det.
        missed = {"all": [{"f": "got_utskick", "op": "not_in", "v": [u.pk]}]}
        self.assertNotIn(spam, set(segments.contacts(self.account, missed, NOW)))

    def test_a_full_account_is_refused(self):
        u = self.sent()
        Segment.objects.bulk_create(
            Segment(account=self.account, name=f"S{n}") for n in range(Segment.MAX_PER_ACCOUNT)
        )
        with self.assertRaises(SegmentError) as caught:
            segments.create_follow_up(u, user=self.anna)
        self.assertEqual(caught.exception.errors, [segments.FULL_TEXT])


# ---------------------------------------------------------------------------
# Vyerna
# ---------------------------------------------------------------------------


def form_rows(*rows, name="Service i höst", **extra):
    """Formulärets fält: rows är (grupp, fält, op, värde, antal, enhet)."""
    data = {"namn": name, **extra}
    for n, (group_key, field, op, value, count, unit) in enumerate(rows):
        data[f"r{n}_g"] = group_key
        data[f"r{n}_f"] = field
        data[f"r{n}_op"] = op
        if value is not None:
            data[f"r{n}_v"] = value
        if count is not None:
            data[f"r{n}_n"] = count
        if unit is not None:
            data[f"r{n}_unit"] = unit
    return data


class BuilderViewTests(SegmentFixture, TestCase):
    def new_url(self):
        return reverse("flamingo:app_segment_new")

    def count_url(self):
        return reverse("flamingo:app_segment_count")

    def test_the_new_page(self):
        response = self.client.get(self.new_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["app_active"], "contacts")
        html = response.content.decode()
        for needle in (
            "+ Villkor",
            "+ Grupp (ELLER)",
            "Matchar just nu",
            'data-sg-count="/flamingo/app/kontakter/segment/antal/"',
            '<option value="field:senaste-service">Senaste service</option>',
            '<option value="consent:sms">Samtycke för sms</option>',
            '<template data-sg-tpl="field:senaste-service">',
            "<template data-sg-row-tpl>",
            "Öppnade mejl (låst)",
            segments.OPENED_LOCKED_TEXT,
            '<summary class="fl-subnav__summary">Kontakter: Listor</summary>',
            "css/flamingo-app-segment.css",
            "js/flamingo-app-segment.js",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        self.assertNotIn("Hemlig", html)
        self.assertNotIn('<template data-sg-tpl="opened">', html)

    def test_saving_the_acceptance_segment_from_the_form(self):
        match = self.person()
        self.with_fields(match, **{"senaste-service": "2026-01-02"})
        recent = self.person()
        self.with_fields(recent, **{"senaste-service": "2026-01-02"})
        Lead.objects.create(account=self.account, contact=recent)
        data = form_rows(
            ("", "field:senaste-service", "before", None, "5", "months"),
            ("", "list", "in", str(self.kunder.pk), None, None),
            ("", "lead", "not_within_days", None, "30", None),
            ("g1", "tag", "in", str(self.vip.pk), None, None),
            ("g1", "consent:sms", "eligible", None, None, None),
            action="save",
        )
        response = self.client.post(self.new_url(), data)
        segment = Segment.objects.get(account=self.account)
        self.assertRedirects(
            response,
            reverse("flamingo:app_segment", args=[segment.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(segment.name, "Service i höst")
        self.assertEqual(segment.created_by, self.anna)
        self.assertEqual(
            segment.rules,
            {
                "all": [
                    {"f": "field:senaste-service", "op": "before_months", "v": 5},
                    {"f": "list", "op": "in", "v": [self.kunder.pk]},
                    {"f": "lead", "op": "not_within_days", "v": 30},
                    {
                        "any": [
                            {"f": "tag", "op": "in", "v": [self.vip.pk]},
                            {"f": "consent", "op": "eligible", "v": {"channel": "sms"}},
                        ]
                    },
                ]
            },
        )
        self.assertEqual((segment.cached_count, segment.cached_sms), (1, 1))
        page = self.client.get(response["Location"])
        self.assertContains(page, "Segmentet Service i höst är skapat.")
        self.assertContains(page, "Senaste service äldre än 5 månader")
        self.assertContains(page, '<option value="months" selected>månader</option>')
        self.assertContains(page, 'name="r3_g" value="g1"')

    def test_the_acceptance_segment_counts_live_while_editing(self):
        match = self.person()
        self.with_fields(match, **{"senaste-service": "2025-12-01"})
        other = self.person(contact_list=self.bromma)
        self.with_fields(other, **{"senaste-service": "2025-12-01"})
        recent = self.person()
        self.with_fields(recent, **{"senaste-service": "2025-12-01"})
        Lead.objects.create(account=self.account, contact=recent)
        rules = service_rules(self.kunder)
        # Medan kunden bygger: första villkoret ensamt, sedan alla tre.
        data = self.client.post(
            self.count_url(),
            json.dumps({"rules": {"all": rules["all"][:1]}}),
            content_type="application/json",
        ).json()
        self.assertEqual(data["total"], 3)
        data = self.client.post(
            self.count_url(), json.dumps({"rules": rules}), content_type="application/json"
        ).json()
        self.assertEqual((data["ok"], data["total"], data["sms"], data["email"]), (True, 1, 1, 0))
        self.assertEqual(data["text"], "1 kan få sms · 0 kan få e-post")
        self.assertEqual(data["lines"][1], "Finns i listan Kunder")
        # Skriptet skickar formuläret som det står.
        form = form_rows(
            ("", "field:senaste-service", "before", None, "5", "months"),
            ("", "list", "in", str(self.kunder.pk), None, None),
            ("", "lead", "not_within_days", None, "30", None),
            ("", "tag", "in", "", None, None),
        )
        data = self.client.post(self.count_url(), form).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["note"], "Villkor 4 räknas inte förrän det är ifyllt.")

    def test_the_page_says_one_contact_and_a_dash_when_it_cannot_count(self):
        # Ordet efter talet byts av skriptet med talet (data-sg-one, data-sg-many).
        unit = '<span data-sg-unit data-sg-one="kontakt" data-sg-many="kontakter">'
        page = self.client.get(reverse("flamingo:app_segment_new"))
        self.assertContains(page, f"<span data-sg-total>0</span> {unit}kontakter</span>")
        with mock.patch.object(
            views, "live_count", return_value={"ok": False, "note": "Nej.", "lines": []}
        ):
            page = self.client.get(reverse("flamingo:app_segment_new"))
        self.assertContains(page, f"<span data-sg-total>-</span> {unit}kontakter</span>")
        one = {"ok": True, "total": 1, "total_text": "1", "text": "x", "note": "", "lines": []}
        with mock.patch.object(views, "live_count", return_value=one):
            page = self.client.get(reverse("flamingo:app_segment_new"))
        self.assertContains(page, f"<span data-sg-total>1</span> {unit}kontakt</span></p>")

    def test_the_count_explains_and_is_limited(self):
        data = self.client.post(
            self.count_url(), json.dumps({"rules": {}}), content_type="application/json"
        ).json()
        self.assertEqual((data["total"], data["note"]), (0, views.NO_RULES_NOTE))
        data = self.client.post(
            self.count_url(),
            json.dumps({"rules": {"all": [{"f": "field:bilar", "op": "lt", "v": "x"}]}}),
            content_type="application/json",
        ).json()
        self.assertFalse(data["ok"])
        self.assertIn("skriv ett tal", data["note"])
        for body in ("inte json", "[1]"):
            response = self.client.post(self.count_url(), body, content_type="application/json")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(self.count_url()).status_code, 405)

    def test_the_count_has_a_limit_per_minute(self):
        with mock.patch("apps.utskick.app_views.segments.timezone.now", return_value=NOW):
            for _ in range(views.COUNT_PER_MINUTE):
                self.assertEqual(self.client.post(self.count_url(), {}).status_code, 200)
            response = self.client.post(self.count_url(), {})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json()["note"], views.LIMIT_TEXT)
        # En rad per konto och minut, nyckeln är kontot.
        row = Counter.objects.get(scope=views.COUNT_SCOPE)
        self.assertEqual((row.key, row.count), (str(self.account.pk), views.COUNT_PER_MINUTE + 1))

    def test_the_count_needs_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.anna)
        response = client.post(self.count_url(), json.dumps({"rules": {}}), "application/json")
        self.assertEqual(response.status_code, 403)

    def test_errors_are_shown_per_row_and_nothing_is_saved(self):
        data = form_rows(
            ("", "list", "in", "", None, None),
            ("", "field:bilar", "gt", "många", None, None),
            name="",
            action="save",
        )
        response = self.client.post(self.new_url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Segment.objects.exists())
        for text in (
            views.NAME_TEXT,
            views.ROWS_TEXT,
            segments.MISSING_TEXTS["list"],
            segments.NUMBER_TEXT,
        ):
            self.assertContains(response, text)
        self.assertContains(response, 'value="många"')
        response = self.client.post(self.new_url(), {"namn": "Tomt", "action": "save"})
        self.assertContains(response, views.EMPTY_TEXT)
        self.assertFalse(Segment.objects.exists())

    def test_the_name_is_unique_per_account(self):
        Segment.objects.create(account=self.account, name="Service i höst")
        Segment.objects.create(account=self.other_account, name="Bromma")
        data = form_rows(("", "kind", "eq", "person", None, None), action="save")
        response = self.client.post(self.new_url(), data)
        self.assertContains(response, views.EXISTS_TEXT)
        data["namn"] = "Bromma"
        response = self.client.post(self.new_url(), data)
        self.assertEqual(response.status_code, 302)

    def test_the_buttons_work_without_the_script(self):
        rows = (
            ("", "list", "in", str(self.kunder.pk), None, None),
            ("g1", "tag", "in", str(self.vip.pk), None, None),
            ("g1", "kind", "eq", "person", None, None),
        )
        response = self.client.post(self.new_url(), form_rows(*rows, action="add_rule"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["items"]), 3)
        self.assertEqual(response.context["next_index"], 4)
        response = self.client.post(self.new_url(), form_rows(*rows, action="add_group"))
        self.assertEqual(response.context["group_count"], 2)
        self.assertEqual(response.context["next_index"], 5)
        response = self.client.post(self.new_url(), form_rows(*rows, action="add_to:g1"))
        self.assertEqual(len(response.context["items"][1]["rows"]), 3)
        response = self.client.post(self.new_url(), form_rows(*rows, action="remove:1"))
        self.assertEqual(len(response.context["items"][1]["rows"]), 1)
        self.assertContains(response, f'<option value="{self.kunder.pk}" selected>Kunder</option>')
        response = self.client.post(
            self.new_url(),
            form_rows(("", "field:senaste-service", "in", None, None, None), action="refresh"),
        )
        self.assertEqual(response.context["items"][0]["row"]["op"], "before")
        self.assertFalse(Segment.objects.exists())

    def test_the_detail_page_saves_and_deletes(self):
        segment = Segment.objects.create(
            account=self.account,
            name="Kunder",
            rules={"all": [{"f": "list", "op": "in", "v": [self.kunder.pk]}]},
        )
        self.people(2)
        url = reverse("flamingo:app_segment", args=[segment.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Finns i listan Kunder")
        self.assertContains(response, f"?segment={segment.pk}")
        segment.refresh_from_db()
        self.assertEqual(segment.cached_count, 2)
        data = form_rows(("", "tag", "not_in", str(self.vip.pk), None, None), name="Utan VIP")
        response = self.client.post(url, {**data, "action": "save"})
        self.assertRedirects(response, url)
        segment.refresh_from_db()
        self.assertEqual(segment.name, "Utan VIP")
        self.assertEqual(segment.rules["all"][0]["f"], "tag")
        draft = self.utskick(
            status=Utskick.Status.DRAFT, audience={"exclude": {"segments": [segment.pk]}}
        )
        response = self.client.post(url, {"action": "delete"})
        self.assertRedirects(response, url)
        self.assertTrue(Segment.objects.filter(pk=segment.pk).exists())
        self.assertContains(self.client.get(url), draft.name)
        Utskick.objects.filter(pk=draft.pk).update(status=Utskick.Status.CANCELLED)
        response = self.client.post(url, {"action": "delete"})
        self.assertRedirects(response, reverse("flamingo:app_lists") + "#segment")
        self.assertFalse(Segment.objects.filter(pk=segment.pk).exists())

    def test_a_saved_rule_that_points_at_something_removed(self):
        gone = ContactList.objects.create(account=self.account, name="Gammal")
        segment = Segment.objects.create(
            account=self.account,
            name="Gammalt",
            rules={
                "all": [
                    {"f": "list", "op": "in", "v": [gone.pk]},
                    {"f": "field:finns-inte", "op": "eq", "v": "x"},
                ]
            },
        )
        gone.delete()
        response = self.client.get(reverse("flamingo:app_segment", args=[segment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Det valda finns inte längre. Välj igen.")
        self.assertContains(response, segments.GONE_FIELD_TEXT)

    def test_staff_in_view_as_builds_for_the_customer(self):
        staff = self.client_for(self.staff)
        data = form_rows(("", "kind", "eq", "company", None, None), action="save")
        response = staff.post(self.new_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Segment.objects.get(account=self.account).created_by, self.staff)

    def test_a_full_account_cannot_add_more(self):
        Segment.objects.bulk_create(
            Segment(account=self.account, name=f"S{n}") for n in range(Segment.MAX_PER_ACCOUNT)
        )
        self.assertContains(self.client.get(self.new_url()), segments.FULL_TEXT)
        data = form_rows(("", "kind", "eq", "company", None, None), name="Ett till", action="save")
        response = self.client.post(self.new_url(), data)
        self.assertContains(response, segments.FULL_TEXT)
        self.assertFalse(Segment.objects.filter(name="Ett till").exists())


class ForeignIdTests(SegmentFixture, TestCase):
    """H.1: ett främmande id i en förfrågans kropp ger 400 och ingen ändring."""

    def setUp(self):
        super().setUp()
        self.theirs_list = ContactList.objects.create(account=self.other_account, name="Hemlig")
        self.theirs_tag = Tag.objects.create(account=self.other_account, name="Hemlig")
        self.theirs_utskick = self.sent("Hemligt", account=self.other_account)
        self.theirs_segment = Segment.objects.create(
            account=self.other_account,
            name="Hemligt",
            rules={"all": [{"f": "contact:phone", "op": "not_empty"}]},
        )

    def test_the_builder_and_the_count(self):
        for field, value in (
            ("list", self.theirs_list.pk),
            ("tag", self.theirs_tag.pk),
            ("got_utskick", self.theirs_utskick.pk),
            ("clicked", self.theirs_utskick.pk),
        ):
            data = form_rows(("", field, "in", str(value), None, None))
            for action in ("save", "add_rule", "refresh"):
                with self.subTest(field=field, action=action):
                    response = self.client.post(
                        reverse("flamingo:app_segment_new"), {**data, "action": action}
                    )
                    self.assertEqual(response.status_code, 400)
            with self.subTest(field=field, view="count"):
                response = self.client.post(reverse("flamingo:app_segment_count"), data)
                self.assertEqual(response.status_code, 400)
                rules = {"all": [{"f": field, "op": "in", "v": [value]}]}
                response = self.client.post(
                    reverse("flamingo:app_segment_count"),
                    json.dumps({"rules": rules}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Segment.objects.filter(account=self.account).exists())
        mine = Segment.objects.create(account=self.account, name="Mitt")
        response = self.client.post(
            reverse("flamingo:app_segment", args=[mine.pk]),
            {
                **form_rows(("", "list", "in", str(self.theirs_list.pk), None, None)),
                "action": "save",
            },
        )
        self.assertEqual(response.status_code, 400)
        mine.refresh_from_db()
        self.assertEqual(mine.rules, {})

    def test_another_accounts_segment_is_404(self):
        url = reverse("flamingo:app_segment", args=[self.theirs_segment.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"action": "delete"}).status_code, 404)
        self.assertTrue(Segment.objects.filter(pk=self.theirs_segment.pk).exists())

    def test_the_mottagare_step_and_its_count(self):
        u = self.utskick(status=Utskick.Status.DRAFT, audience={})
        step = reverse("flamingo:app_utskick_step", args=[u.pk, "mottagare"])
        for field in ("segments", "exclude_segments"):
            with self.subTest(field=field):
                response = self.client.post(
                    step, {"namn": "X", "lists": [self.kunder.pk], field: self.theirs_segment.pk}
                )
                self.assertEqual(response.status_code, 400)
        u.refresh_from_db()
        self.assertEqual(u.audience, {})
        count = reverse("flamingo:app_utskick_count", args=[u.pk])
        self.assertEqual(
            self.client.get(count, {"segments": self.theirs_segment.pk}).status_code, 400
        )

    def test_the_contact_lists_filter(self):
        self.person()
        response = self.client.post(
            reverse("flamingo:app_contacts_bulk"),
            {
                "action": "tag_add",
                "tagg_id": self.vip.pk,
                "alla": "1",
                "segment": self.theirs_segment.pk,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.vip.contacts.exists())
        # I adressraden hoppas ett okänt id över (som listan), och inget läcker.
        response = self.client.get(
            reverse("flamingo:app_contacts"), {"segment": self.theirs_segment.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["filters"].segment)


# ---------------------------------------------------------------------------
# Listor, Kontakter, kortet och Mottagare
# ---------------------------------------------------------------------------


class IntegrationTests(SegmentFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.anna_k = self.person(first_name="Agnes")
        self.bo_k = self.person(first_name="Bodil", contact_list=self.bromma)
        self.segment = Segment.objects.create(
            account=self.account,
            name="Kunder i stan",
            rules={
                "all": [
                    {"f": "list", "op": "in", "v": [self.kunder.pk]},
                    {
                        "any": [
                            {"f": "kind", "op": "eq", "v": "person"},
                            {"f": "contact:email", "op": "not_empty"},
                        ]
                    },
                ]
            },
        )
        segments.refresh(self.segment)

    def test_listor_shows_the_segments(self):
        Segment.objects.create(account=self.other_account, name="Hemligt")
        response = self.client.get(reverse("flamingo:app_lists"))
        self.assertContains(response, "Kunder i stan")
        self.assertContains(response, "Segment · 3 regler")
        self.assertContains(response, reverse("flamingo:app_segment", args=[self.segment.pk]))
        self.assertContains(response, reverse("flamingo:app_segment_new"))
        self.assertNotContains(response, "Hemligt")

    def test_the_contact_list_filters_on_a_segment(self):
        response = self.client.get(reverse("flamingo:app_contacts"), {"segment": self.segment.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Agnes")
        self.assertNotContains(response, "Bodil")
        self.assertContains(response, "segmentet Kunder i stan")
        self.assertContains(response, f'<option value="{self.segment.pk}" selected>')

    def test_the_card_shows_the_segment_chips(self):
        html = self.client.get(reverse("flamingo:app_contact", args=[self.anna_k.pk])).content
        self.assertIn(b"Segment: Kunder i stan", html)
        html = self.client.get(reverse("flamingo:app_contact", args=[self.bo_k.pk])).content
        self.assertNotIn(b"Segment: Kunder i stan", html)

    def test_the_mottagare_step_offers_and_saves_segments(self):
        u = self.utskick(status=Utskick.Status.DRAFT, audience={})
        step = reverse("flamingo:app_utskick_step", args=[u.pk, "mottagare"])
        page = self.client.get(step)
        self.assertContains(page, f'name="segments" value="{self.segment.pk}"')
        self.assertContains(page, f'name="exclude_segments" value="{self.segment.pk}"')
        response = self.client.post(
            step, {"namn": "Höstservice", "segments": [self.segment.pk], "nasta": "kanal"}
        )
        self.assertRedirects(response, reverse("flamingo:app_utskick_step", args=[u.pk, "kanal"]))
        u.refresh_from_db()
        self.assertEqual(u.audience["segments"], [self.segment.pk])
        self.assertFalse(audience.is_empty(u))
        self.assertEqual(audience.describe(u), "Segment Kunder i stan")
        count = reverse("flamingo:app_utskick_count", args=[u.pk])
        data = self.client.get(count).json()
        self.assertEqual((data["total"], data["sms"]), (1, 1))
        data = self.client.get(count, {"urval": "1", "exclude_segments": self.segment.pk}).json()
        self.assertEqual(data["total"], 0)
        self.segment.delete()
        u.refresh_from_db()
        self.assertEqual(audience.describe(u), "Ett borttaget segment")
        self.assertFalse(audience.contacts(u).exists())


# ---------------------------------------------------------------------------
# Vakter
# ---------------------------------------------------------------------------


class GuardTests(SimpleTestCase):
    css = (BASE / "static" / "css" / "flamingo-app-segment.css").read_text("utf-8")
    js = (BASE / "static" / "js" / "flamingo-app-segment.js").read_text("utf-8")
    template = (BASE / "templates" / "flamingo" / "app" / "kontakter" / "segment.html").read_text(
        "utf-8"
    )

    def test_rows_stack_under_760_px_and_the_columns_under_900(self):
        narrow = self.css[self.css.index("@media (max-width:760px)") :]
        self.assertIn(".fl-sg-rule{flex-direction:column;align-items:stretch}", narrow)
        self.assertIn(".fl-sg-rule .fl-input{width:100%}", narrow)
        medium = self.css[self.css.index("@media (max-width:900px)") :]
        self.assertIn(".fl-sg__cols{grid-template-columns:minmax(0,1fr)}", medium)
        self.assertIn(".fl-sg [hidden]{display:none}", self.css)

    def test_the_script_posts_the_form_with_the_csrf_token_and_waits(self):
        for needle in (
            'getAttribute("data-sg-count")',
            '"X-CSRFToken": csrfToken()',
            "new URLSearchParams(new FormData(form))",
            "window.setTimeout(ask, 400)",
            "mine === asked",
            "template[data-sg-row-tpl]",
            "template[data-sg-tpl]",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.js)
        for name in ("data-sg-count", "data-sg-next", "data-sg-items", "data-sg-total"):
            self.assertIn(name, self.template)

    def test_the_count_shows_its_own_errors(self):
        # Granskningen: response.ok ? json : null kastade 429 och 400, så
        # "Räkningen tar en paus" syntes aldrig och gamla siffror stod kvar.
        self.assertNotIn("response.ok ? response.json() : null", self.js)
        for needle in (
            "result.status === 429",
            'total.textContent = "-"',
            'text.textContent = "-"',
            'getAttribute("data-sg-error-text")',
            # Slutkontrollen: ordet stod kvar, "1 kontakter" när talet ändrades.
            'form.querySelector("[data-sg-unit]")',
            'unit.getAttribute(one ? "data-sg-one" : "data-sg-many")',
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.js)
        self.assertIn('data-sg-error-text="{{ count_error_text }}"', self.template)

    def test_each_row_shows_its_number(self):
        # Anteckningarna säger "Villkor 3 och 4 ..."; raderna visar samma nummer.
        self.assertIn(".fl-sg-items{counter-reset:sg-villkor}", self.css)
        self.assertIn(".fl-sg-rule{counter-increment:sg-villkor}", self.css)
        self.assertIn('content:"Villkor " counter(sg-villkor)', self.css)
        row = BASE / "templates" / "flamingo" / "app" / "kontakter" / "_segment_row.html"
        self.assertIn('<p class="fl-sg-rule__num"></p>', row.read_text("utf-8"))

    def test_the_vocabulary_and_the_copy(self):
        self.assertEqual(set(segments.FIELDS), set(segments._COMPILERS))
        texts = [
            *segments.MISSING_TEXTS.values(),
            segments.OPENED_LOCKED_TEXT,
            segments.SHAPE_TEXT,
            segments.FULL_TEXT,
            segments.GONE_FIELD_TEXT,
            views.NAME_TEXT,
            views.EXISTS_TEXT,
            views.EMPTY_TEXT,
            views.IN_USE_TEXT,
            views.LIMIT_TEXT,
            views.COUNT_ERROR_TEXT,
        ]
        typographic = [chr(c) for c in (0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026)]
        for text in texts:
            with self.subTest(text=text):
                self.assertNotIn(chr(0x21), text)
                self.assertFalse([ch for ch in typographic if ch in text])
                self.assertTrue(text.endswith("."))
        for name, ops in segments.FIELDS.items():
            for op in ops:
                self.assertIn(op, segments.OPS, name)

    def test_every_rule_op_in_the_form_reaches_the_compiler(self):
        composed = {"before", "within", "next"}
        for ops in (
            views.TEXT_FORM_OPS,
            views.NUMBER_FORM_OPS,
            views.DATE_FORM_OPS,
            views.CHOICE_FORM_OPS,
            *views.ACTIVITY_FORM_OPS.values(),
        ):
            for op, label in ops:
                with self.subTest(op=op):
                    self.assertTrue(op in segments.OPS or op in composed)
                    self.assertTrue(label)
        self.assertTrue(re.match(r"^[a-z_]+$", views.DEFAULT_FIELD))
