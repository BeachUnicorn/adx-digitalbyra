"""Kontakter S1: modellerna, regler i databasen, hashen, normaliseringen,
behörigheten, menyn, räknarna och kontakternas livscykel (README J S1)."""

import hashlib
import hmac
from datetime import timedelta

from django.core import mail
from django.db import IntegrityError, transaction
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.app_views import APP_NAV
from apps.flamingo.models import Lead

from . import access, contacts, keys, limits, nav, normalize, timeline
from .freemail import is_freemail
from .models import (
    Consent,
    ConsentLog,
    Contact,
    ContactList,
    Counter,
    DpaAcceptance,
    DpaVersion,
    Event,
    FieldDef,
    ListMembership,
    Suppression,
    Switchboard,
    Tag,
    UtskickSettings,
)
from .templatetags.utskick_tags import procent
from .testing import PHONE_ANNA, PHONE_BO, UtskickFixture, make_contact

# ---------------------------------------------------------------------------
# Reglerna i databasen
# ---------------------------------------------------------------------------


class ConstraintTests(UtskickFixture, TestCase):
    def test_phone_and_email_are_unique_per_account_but_blank_is_free(self):
        Contact.objects.create(account=self.account, phone=PHONE_ANNA, source="manual")
        Contact.objects.create(account=self.account, source="manual")
        Contact.objects.create(account=self.account, source="manual")
        Contact.objects.create(account=self.other_account, phone=PHONE_ANNA, source="manual")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Contact.objects.create(account=self.account, phone=PHONE_ANNA, source="manual")
        Contact.objects.create(account=self.account, email="a@exempelror.example", source="manual")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Contact.objects.create(
                account=self.account, email="a@exempelror.example", source="manual"
            )

    def test_one_consent_row_per_channel(self):
        contact = Contact.objects.create(account=self.account, source="manual")
        Consent.objects.create(contact=contact, channel="sms")
        Consent.objects.create(contact=contact, channel="email")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Consent.objects.create(contact=contact, channel="sms")

    def test_suppression_is_unique_per_account_channel_and_hash(self):
        value_hash = keys.value_hash("sms", PHONE_ANNA)
        Suppression.objects.create(
            account=self.account, channel="sms", value_hash=value_hash, reason="stop"
        )
        Suppression.objects.create(
            account=self.account, channel="email", value_hash=value_hash, reason="stop"
        )
        Suppression.objects.create(
            account=self.other_account, channel="sms", value_hash=value_hash, reason="stop"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Suppression.objects.create(
                account=self.account, channel="sms", value_hash=value_hash, reason="link"
            )

    def test_fields_tags_and_lists_are_unique_per_account(self):
        FieldDef.objects.create(account=self.account, key="regnummer", label="Regnummer")
        FieldDef.objects.create(account=self.other_account, key="regnummer", label="Regnummer")
        with self.assertRaises(IntegrityError), transaction.atomic():
            FieldDef.objects.create(account=self.account, key="regnummer", label="Igen")
        Tag.objects.create(account=self.account, name="Kund")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Tag.objects.create(account=self.account, name="Kund")
        ContactList.objects.create(account=self.account, name="Kunder")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContactList.objects.create(account=self.account, name="Kunder")

    def test_at_most_one_field_shows_in_the_list(self):
        FieldDef.objects.create(account=self.account, key="a", label="A", show_in_list=True)
        FieldDef.objects.create(account=self.account, key="b", label="B")
        FieldDef.objects.create(account=self.other_account, key="a", label="A", show_in_list=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FieldDef.objects.create(account=self.account, key="c", label="C", show_in_list=True)

    def test_a_contact_is_in_a_list_once(self):
        contact = Contact.objects.create(account=self.account, source="manual")
        kunder = ContactList.objects.create(account=self.account, name="Kunder")
        ListMembership.objects.create(list=kunder, contact=contact)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ListMembership.objects.create(list=kunder, contact=contact)

    def test_exactly_one_current_dpa_version(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            DpaVersion.objects.create(version="2026-11", text="x", sha256="1" * 64, is_current=True)
        DpaVersion.objects.create(version="2026-11", text="x", sha256="1" * 64)

    def test_a_dpa_version_with_acceptances_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.dpa.delete()

    def test_counter_rows_are_unique_per_scope_key_and_window(self):
        window = limits.hour_window()
        Counter.objects.create(scope="signup_ip", key="abc", window=window, count=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Counter.objects.create(scope="signup_ip", key="abc", window=window, count=1)

    def test_switchboard_is_one_row_and_everything_starts_off(self):
        row = Switchboard.get_solo()
        self.assertEqual(row.pk, Switchboard.SOLO_PK)
        self.assertEqual(Switchboard.get_solo().pk, row.pk)
        self.assertFalse(row.sms_enabled or row.email_enabled)
        self.assertIsNone(row.doi_ready_at)

    def test_new_settings_are_off_and_signup_offers_email_only(self):
        row = access.settings_for(self.other_account)
        self.assertTrue(row.pk)
        fresh = UtskickSettings(account=self.account)
        self.assertFalse(fresh.is_enabled)
        self.assertFalse(fresh.open_tracking)
        self.assertEqual(fresh.sms_window, {"weekday": [9, 20], "weekend": [10, 18]})
        from .models import SignupForm

        form = SignupForm(account=self.account, title="Nyheter")
        self.assertEqual(form.channels, ["email"])
        self.assertFalse(form.is_active)

    def test_str_never_carries_personal_data(self):
        contact = make_contact(self.account, first_name="Anna", phone=PHONE_ANNA)
        for row in (contact, contact.consents.first()):
            text = str(row)
            self.assertNotIn("Anna", text)
            self.assertNotIn("0174", text)


class ConsentLogAppendOnlyTests(UtskickFixture, TestCase):
    def test_a_log_row_is_never_updated_or_deleted_one_by_one(self):
        row = ConsentLog.objects.create(
            account=self.account,
            channel="sms",
            value_hash="x" * 64,
            old_status="missing",
            new_status="yes",
            basis="consent",
            source="manual",
        )
        row.evidence = "ändrat"
        with self.assertRaises(ValueError):
            row.save()
        with self.assertRaises(ValueError):
            row.delete()
        # GDPR-tömningen är en uttrycklig QuerySet.update (contacts.delete_contact).
        ConsentLog.objects.filter(pk=row.pk).update(evidence="")
        self.assertTrue(ConsentLog.objects.filter(pk=row.pk).exists())


# ---------------------------------------------------------------------------
# Hashen och nycklarna
# ---------------------------------------------------------------------------


class HashTests(TestCase):
    @override_settings(UTSKICK_HASH_KEY="test-nyckel")
    def test_hash_is_hmac_sha256_of_channel_and_value_and_stable(self):
        expected = hmac.new(b"test-nyckel", b"sms:+46701740605", hashlib.sha256).hexdigest()
        self.assertEqual(keys.value_hash("sms", "+46701740605"), expected)
        # Fryst: samma nyckel och adress ger alltid samma hash (spärrarna).
        self.assertEqual(
            keys.value_hash("sms", "+46701740605"),
            "81934098de2fa4f63d14e7a2d677d714c52a5d12d0320b54676814d2048d271b",
        )

    @override_settings(UTSKICK_HASH_KEY="test-nyckel")
    def test_channel_case_and_whitespace(self):
        self.assertNotEqual(
            keys.value_hash("sms", "x@y.example"), keys.value_hash("email", "x@y.example")
        )
        self.assertEqual(
            keys.value_hash("email", " Anna@Exempelror.example "),
            keys.value_hash("email", "anna@exempelror.example"),
        )
        # Plustaggen är en del av adressen.
        self.assertNotEqual(
            keys.value_hash("email", "anna+nyhet@exempelror.example"),
            keys.value_hash("email", "anna@exempelror.example"),
        )
        self.assertEqual(keys.value_hash("sms", ""), "")

    def test_the_key_is_not_the_secret_key(self):
        with override_settings(UTSKICK_HASH_KEY="a"):
            first = keys.value_hash("sms", PHONE_ANNA)
        with override_settings(UTSKICK_HASH_KEY="b"):
            second = keys.value_hash("sms", PHONE_ANNA)
        self.assertNotEqual(first, second)
        with override_settings(SECRET_KEY="något-annat", UTSKICK_HASH_KEY="a"):
            self.assertEqual(keys.value_hash("sms", PHONE_ANNA), first)


class FingerprintTests(UtskickFixture, TestCase):
    def test_first_use_stores_the_fingerprints(self):
        self.assertTrue(keys.check_fingerprints())
        row = Switchboard.get_solo()
        self.assertEqual((row.hash_fingerprint, row.link_fingerprint), keys.fingerprints())

    def test_another_key_refuses_writes_and_alerts_the_agency_once(self):
        Switchboard.objects.create(pk=1, hash_fingerprint="f" * 64, link_fingerprint="")
        self.assertFalse(keys.check_fingerprints())
        self.assertFalse(keys.check_fingerprints())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("skiljer sig", mail.outbox[0].subject)
        with self.assertRaises(keys.KeyMismatch):
            keys.require_fingerprints()
        with self.assertRaises(keys.KeyMismatch):
            make_contact(self.account, phone=PHONE_ANNA)
        self.assertFalse(Contact.objects.filter(account=self.account).exists())


# ---------------------------------------------------------------------------
# Normaliseringen
# ---------------------------------------------------------------------------


class NormalizeTests(TestCase):
    def test_swedish_mobile_in_every_common_form(self):
        for raw in ("070-174 06 05", "0701740605", "+46 70 174 06 05", "0046701740605"):
            with self.subTest(raw=raw):
                result = normalize.phone(raw)
                self.assertEqual((result.e164, result.country), ("+46701740605", "SE"))
                self.assertTrue(result.ok)

    def test_a_landline_is_kept_as_text_not_as_the_number(self):
        result = normalize.phone("08-123 456 78")
        self.assertEqual(result.e164, "")
        self.assertEqual(result.landline, "08-123 456 78")
        self.assertTrue(result.ok)

    def test_garbage_is_an_error_and_empty_is_nothing(self):
        self.assertTrue(normalize.phone("12345").error)
        self.assertEqual(normalize.phone("  "), normalize.Phone("", "", "", ""))

    def test_email_is_trimmed_lowercased_and_idna(self):
        self.assertEqual(
            normalize.email("  Anna.Lindqvist+nyhet@Exempelror.EXAMPLE "),
            "anna.lindqvist+nyhet@exempelror.example",
        )
        self.assertEqual(
            normalize.email("anna@exempelrör.example"), "anna@xn--exempelrr-77a.example"
        )
        for bad in ("anna@", "anna", "a@b@c.example", "anna@exempel ror.example"):
            with self.subTest(bad=bad), self.assertRaises(normalize.InvalidValue):
                normalize.email(bad)

    def test_org_number_legal_person_is_kept(self):
        self.assertEqual(
            normalize.org_number("556677-8899"), normalize.OrgNumber("5566778899", False)
        )
        self.assertEqual(normalize.org_number("16556677-8899").value, "5566778899")
        with self.assertRaises(normalize.InvalidValue):
            normalize.org_number("5566")

    def test_a_personnummer_as_org_number_is_never_stored(self):
        self.assertEqual(normalize.org_number("121212-1212"), normalize.OrgNumber("", True))
        self.assertEqual(normalize.org_number("19121212-1212"), normalize.OrgNumber("", True))

    def test_personnummer_detection_uses_luhn_and_month(self):
        for value in ("121212-1212", "19121212-1212", "1212121212", "201212121212"):
            with self.subTest(value=value):
                self.assertTrue(normalize.looks_like_personnummer(value))
        for value in ("121212-1213", "556677-8899", "ABC 123", "2026-10-09", "12121212"):
            with self.subTest(value=value):
                self.assertFalse(normalize.looks_like_personnummer(value))

    def test_a_personnummer_in_a_field_is_refused(self):
        with self.assertRaisesMessage(normalize.InvalidValue, normalize.PERSONNUMMER_TEXT):
            normalize.field_value("text", "121212-1212")

    def test_field_values_by_kind(self):
        self.assertEqual(normalize.field_value("date", "2026-3-5"), "2026-03-05")
        self.assertEqual(normalize.field_value("date", "5/3/2026"), "2026-03-05")
        self.assertEqual(normalize.field_value("number", "1 234,50"), "1234.5")
        self.assertEqual(normalize.field_value("choice", "ja", ["Ja", "Nej"]), "Ja")
        self.assertEqual(normalize.field_value("text", "  ABC 123 "), "ABC 123")
        with self.assertRaises(normalize.InvalidValue):
            normalize.field_value("date", "2026-02-30")
        with self.assertRaises(normalize.InvalidValue):
            normalize.field_value("choice", "kanske", ["Ja", "Nej"])

    def test_first_name_on_the_signup_page(self):
        self.assertEqual(normalize.first_name("  Anna-Lena "), "Anna-Lena")
        self.assertEqual(normalize.first_name("D'Artagnan"), "D'Artagnan")
        for bad in ("Anna2", "<b>", "x" * 41, "anna@exempelror.example"):
            with self.subTest(bad=bad), self.assertRaises(normalize.InvalidValue):
                normalize.first_name(bad)

    def test_masks_show_no_name_and_little_of_the_address(self):
        self.assertEqual(normalize.mask_phone("+46701740567"), "070-*** ** 67")
        self.assertEqual(normalize.mask_email("anna@exempelror.example"), "a***@e***.example")

    def test_freemail(self):
        self.assertTrue(is_freemail("anna@gmail.com"))
        self.assertTrue(is_freemail("Anna@Hotmail.SE"))
        self.assertTrue(is_freemail("mail.telia.com"))
        self.assertFalse(is_freemail("anna@exempelror.example"))
        self.assertFalse(is_freemail(""))

    def test_procent_filter(self):
        self.assertEqual(procent(27.4), "27 %")
        self.assertEqual(procent(4.6), "4,6 %")
        self.assertEqual(procent(3), "3 %")
        self.assertEqual(procent(None), "")


# ---------------------------------------------------------------------------
# Behörigheten och menyn
# ---------------------------------------------------------------------------


class AccessTests(UtskickFixture, TestCase):
    def test_settings_for_an_account_without_a_row_is_an_unsaved_default(self):
        UtskickSettings.objects.filter(account=self.other_account).delete()
        row = access.settings_for(self.other_account)
        self.assertIsNone(row.pk)
        self.assertFalse(row.is_enabled)
        self.assertEqual(row.display_name, "Annanfirma AB")
        self.assertIn("Annanfirma AB", row.consent_text_email)
        self.assertFalse(access.is_enabled(self.other_account))

    def test_enabled_needs_flamingo_and_an_active_customer(self):
        self.assertTrue(access.is_enabled(self.account))
        self.account.is_enabled = False
        self.assertFalse(access.is_enabled(self.account))
        self.account.is_enabled = True
        self.customer.is_active = False
        self.assertFalse(access.is_enabled(self.account))
        self.customer.is_active = True

    def test_dpa_must_be_the_current_version(self):
        self.assertTrue(access.dpa_ok(self.account))
        self.assertTrue(access.can_collect(self.account))
        DpaVersion.objects.filter(pk=self.dpa.pk).update(is_current=False)
        newer = DpaVersion.objects.create(
            version="2026-11", text="Ny", sha256="2" * 64, is_current=True
        )
        self.assertFalse(access.dpa_ok(self.account))
        self.assertFalse(access.can_collect(self.account))
        self.assertEqual(access.collect_block_reason(self.account), access.DPA_NEEDED_TEXT)
        DpaAcceptance.objects.create(account=self.account, version=newer)
        self.assertTrue(access.can_collect(self.account))

    def test_without_a_current_version_the_text_says_so(self):
        DpaVersion.objects.update(is_current=False)
        self.assertEqual(access.collect_block_reason(self.account), access.DPA_MISSING_TEXT)

    def test_the_demo_account_never_needs_a_dpa(self):
        DpaAcceptance.objects.filter(account=self.account).delete()
        self.assertFalse(access.dpa_ok(self.account))
        self.account.is_demo = True
        self.assertTrue(access.dpa_ok(self.account))

    def test_owned_and_owned_ids_never_cross_accounts(self):
        mine = Tag.objects.create(account=self.account, name="Kund")
        theirs = Tag.objects.create(account=self.other_account, name="Kund")
        self.assertEqual(access.owned(Tag, self.account, mine.pk), mine)
        with self.assertRaises(Http404):
            access.owned(Tag, self.account, theirs.pk)
        self.assertEqual(access.owned_ids(Tag, self.account, [str(mine.pk), mine.pk]), [mine.pk])
        self.assertEqual(access.owned_ids(Tag, self.account, []), [])
        for bad in ([mine.pk, theirs.pk], ["x"], [True], [-1], [999999], "1; drop"):
            with self.subTest(bad=bad), self.assertRaises(access.ForeignIds):
                access.owned_ids(Tag, self.account, bad)
        with self.assertRaises(access.ForeignIds):
            access.owned_ids(Tag, self.account, [mine.pk], limit=0)

    def test_owned_ids_through_a_parent(self):
        kunder = ContactList.objects.create(account=self.account, name="Kunder")
        contact = make_contact(self.account, first_name="Anna")
        row = ListMembership.objects.create(list=kunder, contact=contact)
        self.assertEqual(
            access.owned_ids(ListMembership, self.other_account, [], via="list__account"), []
        )
        with self.assertRaises(access.ForeignIds):
            access.owned_ids(ListMembership, self.other_account, [row.pk], via="list__account")

    def test_reserved_and_taken_public_slugs(self):
        from django.core.exceptions import ValidationError

        for slug in ("bekrafta", "val", "integritet", "tack", "utskick", "lp", "app"):
            with self.subTest(slug=slug), self.assertRaises(ValidationError):
                access.validate_public_slug(slug)
        with self.assertRaises(ValidationError):
            access.validate_public_slug("exempelror")
        self.assertEqual(
            access.validate_public_slug("exempelror", exclude_pk=self.settings.pk), "exempelror"
        )
        self.assertEqual(access.suggest_public_slug("Exempelrör AB"), "exempelror-2")
        self.assertEqual(access.suggest_public_slug("Utskick AB"), "utskick-2")

    def test_actor_label_for_staff_in_view_as(self):
        from django.test import RequestFactory

        from apps.flamingo.access import VIEWING_AS, FlamingoAccess

        request = RequestFactory().get("/")
        request.user = self.staff
        request.flamingo = FlamingoAccess(VIEWING_AS, customer=self.customer)
        actor = access.actor_for(request)
        self.assertTrue(actor.staff)
        self.assertEqual(actor.label, "ADX (byra)")
        request.user = self.anna
        request.flamingo = None
        actor = access.actor_for(request)
        self.assertEqual((actor.label, actor.staff), ("Anna Lindqvist", False))


class NavTests(UtskickFixture, TestCase):
    def test_contacts_appear_after_campaigns_only_when_enabled(self):
        items = nav.nav_for(self.account)
        keys_in_order = [key for key, _, _ in items]
        self.assertEqual(keys_in_order.index("contacts"), keys_in_order.index("campaigns") + 1)
        # S2: Utskick direkt efter Kontakter (README C.2), tio länkar.
        self.assertEqual(keys_in_order.index("utskick"), keys_in_order.index("contacts") + 1)
        self.assertEqual(len(items), len(APP_NAV) + 2)
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        self.assertEqual(nav.nav_for(self.account), APP_NAV)
        self.assertEqual(nav.nav_for(None), APP_NAV)

    def test_the_sidebar_shows_kontakter_only_when_enabled(self):
        client = self.client_for(self.anna)
        html = client.get(reverse("flamingo:app")).content.decode()
        self.assertIn(reverse("flamingo:app_contacts"), html)
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        html = client.get(reverse("flamingo:app")).content.decode()
        self.assertNotIn(reverse("flamingo:app_contacts"), html)
        self.assertEqual(client.get(reverse("flamingo:app_contacts")).status_code, 404)

    def test_tabs_mark_the_current_part(self):
        tabs = nav.contacts_tabs("import")
        self.assertEqual(tabs["current_label"], "Import")
        # Första fliken heter som delen: ingen "Kontakter: Kontakter".
        self.assertEqual(nav.contacts_tabs("contacts")["current_label"], "")
        self.assertEqual([t["label"] for t in tabs["tabs"]][0], "Kontakter")
        self.assertEqual(sum(t["current"] for t in tabs["tabs"]), 1)


# ---------------------------------------------------------------------------
# Räknarna
# ---------------------------------------------------------------------------


class LimitTests(TestCase):
    def test_hit_counts_exactly_and_refuses_over_the_limit(self):
        window = limits.hour_window()
        results = [limits.hit("signup_ip", "ip1", window, 3) for _ in range(5)]
        self.assertEqual(results, [False, False, False, True, True])
        self.assertEqual(limits.count("signup_ip", "ip1", window), 5)
        self.assertFalse(limits.hit("signup_ip", "ip2", window, 3))
        self.assertFalse(limits.hit("signup_ip", "ip1", window + timedelta(hours=1), 3))

    def test_windows(self):
        from apps.sms.pricing import STOCKHOLM

        now = timezone.now()
        self.assertEqual(limits.hour_window(now).minute, 0)
        day = limits.day_window(now)
        local = timezone.localtime(day, STOCKHOLM)
        self.assertEqual((local.hour, local.minute), (0, 0))

    def test_purge_keeps_two_days(self):
        now = timezone.now()
        limits.hit("export", "1", limits.hour_window(now - timedelta(days=3)), 10)
        limits.hit("export", "1", limits.hour_window(now), 10)
        self.assertEqual(limits.purge(), 1)
        self.assertEqual(Counter.objects.count(), 1)


# ---------------------------------------------------------------------------
# Kontakterna
# ---------------------------------------------------------------------------


class ContactLifecycleTests(UtskickFixture, TestCase):
    def test_create_normalises_and_adds_missing_consent_rows(self):
        contact = make_contact(
            self.account,
            full_name="Anna Lindqvist",
            phone="070-174 06 01",
            email=" Anna@Exempelror.EXAMPLE",
        )
        self.assertEqual((contact.first_name, contact.last_name), ("Anna", "Lindqvist"))
        self.assertEqual(contact.phone, PHONE_ANNA)
        self.assertEqual(contact.email, "anna@exempelror.example")
        rows = {c.channel: c for c in contact.consents.all()}
        self.assertEqual(set(rows), {"sms", "email"})
        self.assertEqual(rows["sms"].status, "missing")
        self.assertEqual(rows["sms"].value_hash, keys.value_hash("sms", PHONE_ANNA))
        self.assertIn("0701740601", contact.search_text)
        self.assertIn("anna lindqvist", contact.search_text)

    def test_a_landline_goes_to_the_phone_field(self):
        contact = make_contact(self.account, first_name="Bo", phone="08-123 456 78")
        self.assertEqual(contact.phone, "")
        self.assertEqual(contact.fields, {"telefon": "08-123 456 78"})
        self.assertTrue(FieldDef.objects.filter(account=self.account, key="telefon").exists())
        self.assertFalse(contact.consents.exists())

    def test_personnummer_is_refused_in_fields_and_not_stored_as_org_number(self):
        FieldDef.objects.create(account=self.account, key="kundnr", label="Kundnr")
        with self.assertRaises(contacts.ContactError) as caught:
            make_contact(self.account, first_name="Bo", fields={"kundnr": "121212-1212"})
        self.assertEqual(caught.exception.message, normalize.PERSONNUMMER_TEXT)
        contact = make_contact(
            self.account, kind="company", company_name="Bo Firma", org_number="121212-1212"
        )
        self.assertEqual((contact.kind, contact.org_number), ("person", ""))

    def test_duplicates_are_a_form_error(self):
        make_contact(self.account, first_name="Anna", phone=PHONE_ANNA)
        with self.assertRaisesMessage(contacts.ContactError, contacts.DUPLICATE_PHONE):
            make_contact(self.account, first_name="Annan", phone="0701740601")
        make_contact(self.account, email="bo@exempelror.example")
        with self.assertRaisesMessage(contacts.ContactError, contacts.DUPLICATE_EMAIL):
            make_contact(self.account, email="BO@exempelror.example")

    def test_creating_needs_can_collect_and_room(self):
        DpaAcceptance.objects.filter(account=self.account).delete()
        with self.assertRaises(contacts.CollectNotAllowed):
            make_contact(self.account, first_name="Anna")
        DpaAcceptance.objects.create(account=self.account, version=self.dpa)
        UtskickSettings.objects.filter(account=self.account).update(contact_limit=1)
        make_contact(self.account, first_name="Anna")
        self.assertEqual(contacts.room_left(self.account), 0)
        with self.assertRaises(contacts.ContactLimitReached):
            make_contact(self.account, first_name="Bo")

    def test_a_suppressed_address_starts_unsubscribed(self):
        Suppression.objects.create(
            account=self.account,
            channel="sms",
            value_hash=keys.value_hash("sms", PHONE_BO),
            reason="stop",
        )
        contact = make_contact(self.account, first_name="Bo", phone=PHONE_BO)
        self.assertEqual(contact.consents.get(channel="sms").status, "unsubscribed")
        self.assertTrue(
            ConsentLog.objects.filter(contact=contact, new_status="unsubscribed").exists()
        )

    def test_search(self):
        FieldDef.objects.create(account=self.account, key="regnr", label="Regnr", show_in_list=True)
        anna = make_contact(
            self.account, first_name="Anna", phone=PHONE_ANNA, fields={"regnr": "ABC 123"}
        )
        make_contact(self.account, first_name="Bo", phone=PHONE_BO)
        qs = Contact.objects.filter(account=self.account)
        self.assertEqual(list(contacts.search(qs, "070-174 06 01")), [anna])
        self.assertEqual(list(contacts.search(qs, "abc123")), [anna])
        self.assertEqual(list(contacts.search(qs, "ANNA")), [anna])
        self.assertEqual(contacts.search(qs, "").count(), 2)

    def test_match_reports_conflicts_instead_of_overwriting(self):
        anna = make_contact(self.account, phone=PHONE_ANNA, email="anna@exempelror.example")
        bo = make_contact(self.account, phone=PHONE_BO)
        self.assertEqual(contacts.match(self.account, phone=PHONE_ANNA).contact, anna)
        self.assertEqual(
            contacts.match(self.account, phone=PHONE_ANNA, email="bo@exempelror.example").conflict,
            "email_differs",
        )
        self.assertEqual(
            contacts.match(self.account, phone=PHONE_BO, email="anna@exempelror.example").conflict,
            "two_contacts",
        )
        self.assertEqual(contacts.match(self.other_account, phone=PHONE_ANNA).contact, None)
        cleaned = contacts.clean(
            self.account, {"email": "bo@exempelror.example", "first_name": "Bo"}
        )
        self.assertTrue(contacts.fill_from(bo, cleaned))
        bo.refresh_from_db()
        self.assertEqual((bo.first_name, bo.email), ("Bo", "bo@exempelror.example"))
        self.assertEqual(bo.consents.get(channel="email").status, "missing")

    def test_lists_and_tags_stay_inside_the_account(self):
        anna = make_contact(self.account, first_name="Anna")
        other = make_contact(self.other_account, first_name="Annan")
        kunder = ContactList.objects.create(account=self.account, name="Kunder")
        self.assertEqual(contacts.add_to_list(kunder, [anna, other.pk]), 1)
        self.assertEqual(contacts.add_to_list(kunder, [anna]), 0)
        tag = Tag.objects.create(account=self.account, name="Vip")
        self.assertEqual(contacts.add_tag(tag, [anna.pk, other.pk]), 1)
        self.assertEqual(list(tag.contacts.all()), [anna])
        self.assertEqual(contacts.remove_tag(tag, [anna]), 1)
        self.assertEqual(contacts.remove_from_list(kunder, [anna]), 1)

    def test_timeline_merges_sources_and_only_shows_the_accounts_leads(self):
        anna = make_contact(self.account, first_name="Anna", phone=PHONE_ANNA)
        from . import consent

        consent.set_status(anna, "sms", "existing", source="manual", evidence="kassan")
        contacts.record_event(anna, Event.IMPORTED, data={"import": 12})
        Lead.objects.create(account=self.account, name="Anna", contact=anna)
        Lead.objects.create(account=self.other_account, name="Fel konto", contact=anna)
        page = timeline.for_contact(anna)
        kinds = [item.kind for item in page.items]
        self.assertEqual(sorted(kinds), ["consent", "imported", "lead"])
        lead_item = next(i for i in page.items if i.kind == "lead")
        self.assertEqual(lead_item.link_label, "Öppna i Inkorgen")
        anna.refresh_from_db()
        self.assertEqual(anna.last_activity_kind, Event.IMPORTED)

    def test_timeline_pages(self):
        anna = make_contact(self.account, first_name="Anna")
        now = timezone.now()
        for i in range(35):
            contacts.record_event(anna, Event.SIGNUP, at=now - timedelta(minutes=i))
        first = timeline.for_contact(anna, page=1)
        second = timeline.for_contact(anna, page=2)
        self.assertEqual((len(first.items), first.has_next), (30, True))
        self.assertEqual((len(second.items), second.has_next), (5, False))
        self.assertTrue(second.has_previous)

    def test_gdpr_export_and_delete_keep_the_proof(self):
        from . import consent

        anna = make_contact(
            self.account, first_name="Anna", phone=PHONE_ANNA, email="anna@exempelror.example"
        )
        consent.set_status(anna, "sms", "yes", source="manual", evidence="kassan, från 2024")
        Lead.objects.create(account=self.account, name="Anna", contact=anna)
        data = contacts.export_contact(anna)
        self.assertEqual(data["kontakt"]["mobil"], PHONE_ANNA)
        self.assertEqual(len(data["förfrågningar"]), 1)
        self.assertTrue(data["samtyckeslogg"])

        summary = contacts.delete_contact(anna)
        self.assertEqual(summary, {"leads": 1, "suppressed": 2})
        self.assertFalse(Contact.objects.filter(pk=anna.pk).exists())
        self.assertFalse(Lead.objects.filter(account=self.account, name="Anna").exists())
        logs = ConsentLog.objects.filter(account=self.account)
        self.assertTrue(logs.exists())
        self.assertTrue(all(row.contact_id is None and row.evidence == "" for row in logs))
        self.assertEqual(
            set(Suppression.objects.filter(account=self.account).values_list("reason", flat=True)),
            {"erasure"},
        )
        # Spärren gör att personen inte kommer tillbaka som ny via import.
        again = make_contact(self.account, phone=PHONE_ANNA)
        self.assertEqual(again.consents.get(channel="sms").status, "unsubscribed")

    def test_delete_without_suppression_or_leads(self):
        anna = make_contact(self.account, first_name="Anna", phone=PHONE_ANNA)
        Lead.objects.create(account=self.account, name="Anna", contact=anna)
        summary = contacts.delete_contact(anna, delete_leads=False, suppress=False)
        self.assertEqual(summary, {"leads": 0, "suppressed": 0})
        self.assertTrue(Lead.objects.filter(name="Anna", contact__isnull=True).exists())
        self.assertFalse(Suppression.objects.exists())

    def test_exports_are_logged_and_limited_per_day(self):
        from .access import Actor

        for _ in range(contacts.EXPORTS_PER_DAY):
            self.assertTrue(contacts.reserve_export(self.account))
        self.assertFalse(contacts.reserve_export(self.account))
        self.assertTrue(contacts.reserve_export(self.other_account))
        row = contacts.log_export(self.account, Actor(user=self.staff, staff=True), "contacts", 12)
        self.assertTrue(row.as_staff)
        self.assertEqual(row.rows, 12)
