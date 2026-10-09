"""Kontakter S1: samtycket. Övergångarna, att importen aldrig vänder eller
lyfter ett val, declined, company, bindningen till adressen och
change_address, vem som får vad per syfte, och att loggen bara växer
(README B.1, H.5, H.6, J S1)."""

from django.test import TestCase

from . import consent, contacts, keys, suppression
from .access import Actor
from .models import Consent, ConsentLog, Contact, Suppression
from .testing import PHONE_ANNA, PHONE_BO, PHONE_CILLA, UtskickFixture, make_contact

S = Consent.Source
YES, EXISTING, COMPANY, PENDING = "yes", "existing", "company", "pending"
MISSING, DECLINED, UNSUBSCRIBED = "missing", "declined", "unsubscribed"
ALL = (YES, EXISTING, COMPANY, PENDING, MISSING, DECLINED, UNSUBSCRIBED)


class TransitionTableTests(TestCase):
    """check_transition, rad för rad (consent.py:s docstring)."""

    def check(self, old, new, source, **kwargs):
        kwargs.setdefault("channel", "email")
        return consent.check_transition(old, new, source, **kwargs)

    def test_anyone_may_opt_someone_out(self):
        for old in (YES, EXISTING, COMPANY, PENDING, MISSING, DECLINED):
            for source in (S.MANUAL, S.IMPORT, S.API, S.STOP, S.LINK, S.PREFERENCE):
                with self.subTest(old=old, source=source):
                    self.assertEqual(self.check(old, UNSUBSCRIBED, source), "")
                    self.assertEqual(self.check(old, DECLINED, source), "")

    def test_customer_sources_only_lift_missing_or_company(self):
        for source in (S.IMPORT, S.MANUAL, S.API):
            for new in (YES, EXISTING):
                with self.subTest(source=source, new=new):
                    self.assertEqual(self.check(MISSING, new, source), "")
                    self.assertEqual(self.check(COMPANY, new, source), "")
                    self.assertEqual(self.check(PENDING, new, source), "locked")
                    self.assertEqual(self.check(DECLINED, new, source), "locked")
                    self.assertEqual(self.check(UNSUBSCRIBED, new, source), "locked")
                    self.assertEqual(
                        self.check(MISSING, new, source, suppressed=True), "suppressed"
                    )
            self.assertEqual(self.check(YES, EXISTING, source), "not_allowed")
            self.assertEqual(self.check(EXISTING, YES, source), "not_allowed")
            self.assertEqual(self.check(MISSING, PENDING, source), "not_allowed")

    def test_only_a_proof_leaves_unsubscribed(self):
        for new in (YES, EXISTING, MISSING, DECLINED, COMPANY):
            with self.subTest(new=new):
                self.assertEqual(self.check(UNSUBSCRIBED, new, S.MANUAL), "locked")
        self.assertEqual(self.check(UNSUBSCRIBED, YES, S.DOI, proved=True, suppressed=True), "")
        self.assertEqual(self.check(UNSUBSCRIBED, YES, S.CONFIRM, proved=True), "")

    def test_forms_start_a_confirmation_but_never_downgrade(self):
        for source in (S.SIGNUP, S.LP_FORM, S.PREFERENCE):
            for old in (MISSING, DECLINED, UNSUBSCRIBED):
                with self.subTest(source=source, old=old):
                    self.assertEqual(self.check(old, PENDING, source), "")
            for old in (YES, EXISTING, COMPANY):
                with self.subTest(source=source, old=old):
                    self.assertEqual(self.check(old, PENDING, source), "already")
        self.assertEqual(self.check(MISSING, PENDING, S.SIGNUP, suppressed=True), "")

    def test_the_lp_checkbox_gives_sms_yes_but_not_over_a_choice(self):
        self.assertEqual(self.check(MISSING, YES, S.LP_FORM, channel="sms"), "")
        self.assertEqual(self.check(EXISTING, YES, S.LP_FORM, channel="sms"), "")
        for old in (PENDING, DECLINED, UNSUBSCRIBED):
            with self.subTest(old=old):
                self.assertEqual(self.check(old, YES, S.LP_FORM, channel="sms"), "locked")
        self.assertEqual(
            self.check(MISSING, YES, S.LP_FORM, channel="sms", suppressed=True), "suppressed"
        )
        self.assertEqual(self.check(MISSING, YES, S.SIGNUP), "not_allowed")

    def test_company_is_email_only_and_only_from_missing(self):
        self.assertEqual(self.check(MISSING, COMPANY, S.MANUAL), "")
        self.assertEqual(self.check(MISSING, COMPANY, S.MANUAL, channel="sms"), "not_allowed")
        self.assertEqual(self.check(DECLINED, COMPANY, S.MANUAL), "locked")
        self.assertEqual(self.check(YES, COMPANY, S.MANUAL), "not_allowed")
        self.assertEqual(self.check(MISSING, COMPANY, S.MANUAL, suppressed=True), "suppressed")

    def test_missing_is_only_reached_by_an_address_change(self):
        for old in (YES, EXISTING, PENDING, DECLINED, UNSUBSCRIBED):
            with self.subTest(old=old):
                self.assertEqual(self.check(old, MISSING, S.ADDRESS), "")
                self.assertIn(self.check(old, MISSING, S.MANUAL), ("not_allowed", "locked"))

    def test_every_pair_has_an_answer(self):
        for old in ALL:
            for new in ALL:
                for source in S.values:
                    result = self.check(old, new, source)
                    self.assertIn(
                        result, ("", "locked", "suppressed", "not_allowed", "already"), (old, new)
                    )


class SetStatusTests(UtskickFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.anna = make_contact(
            self.account,
            first_name="Anna",
            phone=PHONE_ANNA,
            email="anna@exempelror.example",
        )
        self.actor = Actor(user=self.staff, label="ADX (Giovanni)", staff=True)

    def status(self, channel="sms", contact=None):
        return (contact or self.anna).consents.get(channel=channel).status

    def test_manual_yes_needs_evidence_and_writes_the_proof(self):
        out = consent.set_status(self.anna, "sms", YES, source=S.MANUAL, actor=self.actor)
        self.assertEqual(out.refused, "evidence")
        out = consent.set_status(
            self.anna,
            "sms",
            YES,
            source=S.MANUAL,
            actor=self.actor,
            evidence="kassan, från 2024",
            text_shown="Ja, jag vill få erbjudanden från Exempelrör via sms.",
        )
        self.assertTrue(out.ok and out.changed)
        row = self.anna.consents.get(channel="sms")
        self.assertEqual((row.status, row.basis), (YES, "consent"))
        self.assertEqual(row.value_hash, keys.value_hash("sms", PHONE_ANNA))
        log = ConsentLog.objects.get(contact=self.anna)
        self.assertEqual((log.old_status, log.new_status, log.source), (MISSING, YES, "manual"))
        self.assertEqual(
            (log.by_label, log.by_staff, log.by_user), ("ADX (Giovanni)", True, self.staff)
        )
        self.assertEqual(log.evidence, "kassan, från 2024")
        self.assertEqual(log.value_hash, row.value_hash)

    def test_the_same_status_writes_nothing(self):
        consent.set_status(self.anna, "sms", EXISTING, source=S.IMPORT, evidence="kassan")
        out = consent.set_status(self.anna, "sms", EXISTING, source=S.IMPORT, evidence="kassan")
        self.assertTrue(out.ok)
        self.assertFalse(out.changed)
        self.assertEqual(ConsentLog.objects.filter(contact=self.anna).count(), 1)

    def test_import_never_flips_a_persons_choice(self):
        for status, source in ((DECLINED, S.PREFERENCE), (UNSUBSCRIBED, S.STOP)):
            contact = make_contact(
                self.account, phone=PHONE_BO if status == DECLINED else PHONE_CILLA
            )
            consent.set_status(contact, "sms", status, source=source)
            for new in (YES, EXISTING):
                with self.subTest(status=status, new=new):
                    out = consent.set_status(contact, "sms", new, source=S.IMPORT, evidence="fil")
                    self.assertEqual(out.refused, "locked")
                    self.assertEqual(self.status(contact=contact), status)

    def test_import_never_lifts_a_suppression(self):
        suppression.add(
            self.account, "email", keys.value_hash("email", "ny@exempelror.example"), "stop"
        )
        contact = make_contact(self.account, email="ny@exempelror.example")
        self.assertEqual(self.status("email", contact), UNSUBSCRIBED)
        out = consent.set_status(contact, "email", YES, source=S.IMPORT, evidence="fil")
        self.assertFalse(out.ok)
        self.assertTrue(Suppression.objects.filter(account=self.account, channel="email").exists())
        # Ett formulär börjar en bekräftelse men lyfter inget förrän klicket.
        out = consent.set_status(contact, "email", PENDING, source=S.SIGNUP)
        self.assertTrue(out.ok)
        self.assertTrue(Suppression.objects.filter(account=self.account, channel="email").exists())
        self.assertFalse(consent.eligible(contact, "email", consent.INFORMATION))

    def test_a_confirmation_click_lifts_the_suppression(self):
        consent.set_status(self.anna, "email", UNSUBSCRIBED, source=S.LINK)
        self.assertTrue(suppression.is_suppressed(self.account, "email", "anna@exempelror.example"))
        consent.set_status(self.anna, "email", PENDING, source=S.SIGNUP, text_shown="Ja")
        with self.assertRaises(ValueError):
            consent.set_status(self.anna, "email", YES, source=S.IMPORT, proved=True)
        out = consent.set_status(self.anna, "email", YES, source=S.DOI, proved=True)
        self.assertTrue(out.ok and out.lifted)
        self.assertFalse(
            suppression.is_suppressed(self.account, "email", "anna@exempelror.example")
        )
        self.assertTrue(consent.eligible(self.anna, "email", consent.REKLAM))

    def test_unsubscribed_always_adds_a_suppression_with_the_reason(self):
        consent.set_status(self.anna, "sms", UNSUBSCRIBED, source=S.STOP)
        row = Suppression.objects.get(account=self.account, channel="sms")
        self.assertEqual(row.reason, "stop")
        self.assertEqual(row.value_hash, keys.value_hash("sms", PHONE_ANNA))
        chip = consent.chip(self.anna, "sms", self.anna.consents.get(channel="sms"))
        self.assertEqual(chip["label"], "Avregistrerad (STOPP)")

    def test_suppress_by_address_reaches_the_contact_even_when_disabled(self):
        from .models import UtskickSettings

        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        _, contact = suppression.suppress(
            self.account, "sms", PHONE_ANNA, reason="stop", source=S.STOP
        )
        self.assertEqual(contact, self.anna)
        self.assertEqual(self.status(), UNSUBSCRIBED)
        _, nobody = suppression.suppress(
            self.account, "sms", "+46701740699", reason="stop", source=S.STOP
        )
        self.assertIsNone(nobody)
        self.assertTrue(suppression.is_suppressed(self.account, "sms", "+46701740699"))
        self.assertFalse(suppression.is_suppressed(self.other_account, "sms", "+46701740699"))

    def test_declined_stops_reklam_but_not_information(self):
        consent.set_status(self.anna, "sms", EXISTING, source=S.IMPORT, evidence="kassan")
        consent.set_status(self.anna, "sms", DECLINED, source=S.PREFERENCE)
        self.assertFalse(consent.eligible(self.anna, "sms", consent.REKLAM))
        self.assertEqual(consent.ineligible_reason(self.anna, "sms", consent.REKLAM), "declined")
        self.assertTrue(consent.eligible(self.anna, "sms", consent.INFORMATION))
        self.assertFalse(Suppression.objects.exists())

    def test_no_address_is_refused(self):
        bo = make_contact(self.account, first_name="Bo")
        out = consent.set_status(bo, "sms", YES, source=S.MANUAL, evidence="x")
        self.assertEqual(out.refused, "no_address")
        self.assertFalse(bo.consents.exists())

    def test_a_process_with_another_key_cannot_write_consent(self):
        from .models import Switchboard

        Switchboard.objects.update_or_create(pk=1, defaults={"hash_fingerprint": "e" * 64})
        keys.forget_verified()
        with self.assertRaises(keys.KeyMismatch):
            consent.set_status(self.anna, "sms", DECLINED, source=S.MANUAL)
        self.assertEqual(self.status(), MISSING)


class CompanyTests(UtskickFixture, TestCase):
    def company(self, email="info@exempelror.example", org="556677-8899", **extra):
        return make_contact(
            self.account,
            kind="company",
            company_name="Exempelrör AB",
            org_number=org,
            email=email,
            **extra,
        )

    def test_a_company_with_org_number_and_own_domain_gets_company(self):
        contact = self.company(phone=PHONE_ANNA)
        self.assertEqual(contact.consents.get(channel="email").status, COMPANY)
        self.assertEqual(contact.consents.get(channel="email").basis, "company")
        # Sms till företag kräver ja eller befintlig kund.
        self.assertEqual(contact.consents.get(channel="sms").status, MISSING)
        self.assertTrue(consent.eligible(contact, "email", consent.REKLAM))
        self.assertFalse(consent.eligible(contact, "sms", consent.REKLAM))
        chip = consent.chip(contact, "email", contact.consents.get(channel="email"))
        self.assertEqual(chip["label"], "E-post (företag)")

    def test_freemail_never_gets_company(self):
        contact = self.company(email="exempelror@gmail.com")
        self.assertEqual(contact.consents.get(channel="email").status, MISSING)
        self.assertFalse(consent.eligible(contact, "email", consent.REKLAM))

    def test_without_a_legal_org_number_it_is_a_person(self):
        contact = self.company(org="121212-1212")
        self.assertEqual(contact.kind, Contact.Kind.PERSON)
        self.assertEqual(contact.consents.get(channel="email").status, MISSING)

    def test_company_only_from_missing_and_reverts_when_it_no_longer_applies(self):
        contact = make_contact(self.account, email="info@exempelror.example")
        consent.set_status(contact, "email", DECLINED, source=S.MANUAL)
        contacts.update(contact, {"kind": "company", "org_number": "556677-8899"})
        self.assertEqual(contact.consents.get(channel="email").status, DECLINED)

        other = self.company(email="kontor@annanfirma.example")
        self.assertEqual(other.consents.get(channel="email").status, COMPANY)
        contacts.update(other, {"kind": "person"})
        self.assertEqual(other.consents.get(channel="email").status, MISSING)
        contacts.update(other, {"kind": "company"})
        self.assertEqual(other.consents.get(channel="email").status, COMPANY)

    def test_a_suppressed_company_address_stays_unsubscribed(self):
        suppression.add(
            self.account, "email", keys.value_hash("email", "info@exempelror.example"), "complaint"
        )
        contact = self.company()
        self.assertEqual(contact.consents.get(channel="email").status, UNSUBSCRIBED)
        self.assertFalse(consent.eligible(contact, "email", consent.INFORMATION))


class AddressBindingTests(UtskickFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.anna = make_contact(
            self.account, first_name="Anna", phone=PHONE_ANNA, email="anna@exempelror.example"
        )
        consent.set_status(self.anna, "sms", YES, source=S.MANUAL, evidence="kassan")
        consent.set_status(self.anna, "email", YES, source=S.MANUAL, evidence="kassan")

    def test_a_new_number_resets_consent_and_logs_it(self):
        self.assertTrue(contacts.change_address(self.anna, "sms", "070-174 06 02"))
        row = self.anna.consents.get(channel="sms")
        self.assertEqual(row.status, MISSING)
        self.assertEqual(row.value_hash, keys.value_hash("sms", PHONE_BO))
        log = ConsentLog.objects.filter(contact=self.anna, channel="sms").first()
        self.assertEqual((log.source, log.source_detail), ("address", "Adressen ändrades"))
        self.assertFalse(consent.eligible(self.anna, "sms", consent.REKLAM))
        self.assertTrue(consent.eligible(self.anna, "email", consent.REKLAM))
        self.assertFalse(contacts.change_address(self.anna, "sms", PHONE_BO))

    def test_a_new_suppressed_address_becomes_unsubscribed(self):
        suppression.add(
            self.account, "email", keys.value_hash("email", "ny@exempelror.example"), "link"
        )
        contacts.change_address(self.anna, "email", "Ny@Exempelror.example")
        self.assertEqual(self.anna.email, "ny@exempelror.example")
        self.assertEqual(self.anna.consents.get(channel="email").status, UNSUBSCRIBED)

    def test_a_bounced_address_is_fresh_after_a_change(self):
        Contact.objects.filter(pk=self.anna.pk).update(email_state="bounced")
        self.anna.refresh_from_db()
        self.assertEqual(
            consent.ineligible_reason(self.anna, "email", consent.INFORMATION), "bounced"
        )
        contacts.change_address(self.anna, "email", "anna2@exempelror.example")
        self.assertEqual(self.anna.email_state, "ok")

    def test_removing_an_address(self):
        contacts.change_address(self.anna, "sms", "")
        row = self.anna.consents.get(channel="sms")
        self.assertEqual((row.status, row.value_hash), (MISSING, ""))
        self.assertEqual(
            consent.ineligible_reason(self.anna, "sms", consent.INFORMATION), "no_address"
        )

    def test_errors_from_change_address(self):
        make_contact(self.account, phone=PHONE_BO)
        with self.assertRaisesMessage(contacts.ContactError, contacts.DUPLICATE_PHONE):
            contacts.change_address(self.anna, "sms", PHONE_BO)
        self.anna.refresh_from_db()
        self.assertEqual(self.anna.phone, PHONE_ANNA)
        with self.assertRaisesMessage(contacts.ContactError, contacts.NOT_MOBILE):
            contacts.change_address(self.anna, "sms", "08-123 456 78")

    def test_consent_is_bound_to_the_address_it_was_given_for(self):
        # En adress som ändrats förbi change_address (som aldrig ska hända)
        # bär inte med sig samtycket.
        Contact.objects.filter(pk=self.anna.pk).update(phone=PHONE_CILLA)
        self.anna.refresh_from_db()
        self.assertEqual(consent.ineligible_reason(self.anna, "sms", consent.REKLAM), "no_consent")

    def test_update_moves_addresses_through_change_address(self):
        contacts.update(self.anna, {"email": "anna@annanfirma.example", "last_name": "Berg"})
        self.assertEqual(self.anna.last_name, "Berg")
        self.assertEqual(self.anna.consents.get(channel="email").status, MISSING)
        self.assertEqual(self.anna.consents.get(channel="sms").status, YES)


class EligibilityTests(UtskickFixture, TestCase):
    def test_purposes_and_the_database_filter_agree(self):
        people = {}
        specs = {
            "yes": (YES, S.MANUAL, {"evidence": "x"}),
            "existing": (EXISTING, S.IMPORT, {"evidence": "x"}),
            "pending": (PENDING, S.SIGNUP, {}),
            "declined": (DECLINED, S.PREFERENCE, {}),
            "unsubscribed": (UNSUBSCRIBED, S.LINK, {}),
            "missing": (None, None, {}),
        }
        for i, (name, (status, source, extra)) in enumerate(specs.items()):
            contact = make_contact(
                self.account,
                first_name=name,
                phone=f"+4670174061{i}",
                email=f"{name}@exempelror.example",
            )
            if status:
                for channel in ("sms", "email"):
                    if status == PENDING and channel == "sms":
                        continue
                    out = consent.set_status(contact, channel, status, source=source, **extra)
                    self.assertTrue(out.ok, (name, channel, out.refused))
            people[name] = contact
        bounced = make_contact(self.account, first_name="studs", email="studs@exempelror.example")
        consent.set_status(bounced, "email", YES, source=S.MANUAL, evidence="x")
        Contact.objects.filter(pk=bounced.pk).update(email_state="bounced")
        people["bounced"] = Contact.objects.get(pk=bounced.pk)
        make_contact(self.other_account, first_name="annan", phone="+46701740619")

        expected = {
            ("sms", consent.REKLAM): {"yes", "existing"},
            ("email", consent.REKLAM): {"yes", "existing"},
            ("sms", consent.INFORMATION): {"yes", "existing", "pending", "declined", "missing"},
            ("email", consent.INFORMATION): {"yes", "existing", "pending", "declined", "missing"},
        }
        for (channel, purpose), names in expected.items():
            with self.subTest(channel=channel, purpose=purpose):
                python = {n for n, c in people.items() if consent.eligible(c, channel, purpose)}
                self.assertEqual(python, names)
                qs = consent.eligible_contacts(
                    Contact.objects.filter(account=self.account), channel, purpose
                )
                self.assertEqual({c.first_name for c in qs}, names)

        self.assertEqual(
            consent.ineligible_reason(people["pending"], "email", consent.REKLAM), "pending_doi"
        )
        self.assertEqual(
            consent.ineligible_reason(people["missing"], "sms", consent.REKLAM), "no_consent"
        )
        self.assertEqual(
            consent.ineligible_reason(people["unsubscribed"], "sms", consent.INFORMATION),
            "suppressed",
        )

    def test_a_suppression_wins_over_any_status(self):
        anna = make_contact(self.account, phone=PHONE_ANNA)
        consent.set_status(anna, "sms", YES, source=S.MANUAL, evidence="x")
        Suppression.objects.create(
            account=self.account,
            channel="sms",
            value_hash=keys.value_hash("sms", PHONE_ANNA),
            reason="stop",
        )
        self.assertFalse(consent.eligible(anna, "sms", consent.REKLAM))
        self.assertFalse(consent.eligible(anna, "sms", consent.INFORMATION))
        qs = Contact.objects.filter(account=self.account)
        self.assertFalse(consent.eligible_contacts(qs, "sms", consent.REKLAM).exists())


class ConsentLogGrowsOnlyTests(UtskickFixture, TestCase):
    def test_every_change_is_one_new_row_and_old_rows_never_change(self):
        anna = make_contact(self.account, phone=PHONE_ANNA)
        consent.set_status(anna, "sms", EXISTING, source=S.IMPORT, evidence="kassan")
        first = ConsentLog.objects.get(contact=anna)
        consent.set_status(anna, "sms", DECLINED, source=S.MANUAL, evidence="per telefon")
        consent.set_status(anna, "sms", UNSUBSCRIBED, source=S.STOP)
        rows = list(ConsentLog.objects.filter(contact=anna).order_by("at", "pk"))
        self.assertEqual(
            [(r.old_status, r.new_status) for r in rows],
            [(MISSING, EXISTING), (EXISTING, DECLINED), (DECLINED, UNSUBSCRIBED)],
        )
        first_again = ConsentLog.objects.get(pk=first.pk)
        self.assertEqual(
            (first_again.new_status, first_again.evidence, first_again.at),
            (first.new_status, first.evidence, first.at),
        )
        with self.assertRaises(ValueError):
            first_again.save()
