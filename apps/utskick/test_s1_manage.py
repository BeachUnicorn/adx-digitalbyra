"""Byråns sida av Kontakter och utskick (README I.1 "Manage views", B.1,
D.8, E.7): kundkortet, aktiveringen, översikten, nödstoppet och
klarmarkeringarna, publiceringen av biträdesavtalet och "Avsluta utskick
och radera allt". Ingen kund mejlas någonstans.
"""

import re
from unittest import mock

from django.contrib.messages import get_messages
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount
from apps.projects.models import Customer
from apps.website.models import Block, BlockPage

from . import access, keys, manage_views
from . import consent as consents
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    ConsentLog,
    Contact,
    ContactList,
    DpaVersion,
    ImportJob,
    SignupForm,
    Suppression,
    Switchboard,
    Tag,
    UtskickSettings,
)
from .testing import PHONE_ANNA, PHONE_BO, UtskickFixture, make_contact

AGENCY = {"INQUIRY_NOTIFICATION_EMAIL": "byran@adx.example"}


class ManageFixture(UtskickFixture):
    def staff_client(self):
        client = Client()
        client.force_login(self.staff)
        return client

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def new_customer(self, name="Nykund AB", flamingo=True):
        customer = Customer.objects.create(name=name)
        account = FlamingoAccount.objects.create(customer=customer, is_enabled=flamingo)
        return customer, account


class AccessTests(ManageFixture, TestCase):
    def test_only_the_agency_reaches_the_pages(self):
        client = Client()
        client.force_login(self.anna)
        urls = [
            ("get", reverse("manage:utskick_overview")),
            ("post", reverse("manage:utskick_switch")),
            ("post", reverse("manage:utskick_dpa_publish")),
            ("post", reverse("manage:utskick_customer_update", args=[self.customer.pk])),
            ("get", reverse("manage:utskick_customer_end", args=[self.customer.pk])),
            ("post", reverse("manage:utskick_customer_end", args=[self.customer.pk])),
        ]
        for method, url in urls:
            with self.subTest(url=url, method=method):
                response = getattr(client, method)(url, {"namn": self.customer.name})
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("/manage/", response["Location"])
        self.assertTrue(UtskickSettings.objects.get(pk=self.settings.pk).is_enabled)
        self.assertEqual(Switchboard.objects.filter(sms_enabled=True).count(), 0)


@override_settings(**AGENCY)
class CustomerCardTests(ManageFixture, TestCase):
    def test_the_card_shows_the_utskick_part(self):
        response = self.staff_client().get(
            reverse("manage:customer_detail", args=[self.customer.pk])
        )
        self.assertContains(response, 'id="utskick"')
        self.assertContains(response, "Kontakter och utskick")
        self.assertContains(
            response, reverse("manage:utskick_customer_end", args=[self.customer.pk])
        )
        # Tal med mellanslag, och knappen säger vad den sparar.
        self.assertContains(response, "0 av högst 25\u00a0000 kontakter.")
        self.assertContains(response, "Spara inställningarna")
        self.assertNotContains(response, "Spara utskick")
        self.assertContains(response, "krävs när du stoppar sändningen")

    def test_enabling_needs_flamingo(self):
        customer, account = self.new_customer(flamingo=False)
        url = reverse("manage:utskick_customer_update", args=[customer.pk])
        response = self.staff_client().post(url, {"is_enabled": "on"})
        self.assertIn(manage_views.FLAMINGO_OFF_TEXT, self.messages(response))
        self.assertFalse(UtskickSettings.objects.filter(account=account).exists())

    def test_enabling_creates_the_row_and_mails_nobody(self):
        customer, account = self.new_customer(name="Exempel VVS AB")
        url = reverse("manage:utskick_customer_update", args=[customer.pk])
        response = self.staff_client().post(url, {"is_enabled": "on", "display_name": ""})
        self.assertRedirects(
            response,
            reverse("manage:customer_detail", args=[customer.pk]) + "#utskick",
            fetch_redirect_response=False,
        )
        row = UtskickSettings.objects.get(account=account)
        self.assertTrue(row.is_enabled)
        self.assertEqual(row.display_name, "Exempel VVS AB")
        self.assertEqual(row.public_slug, "exempel-vvs")
        self.assertEqual(row.enabled_by, self.staff)
        self.assertEqual(
            row.consent_text_sms, "Ja, jag vill få erbjudanden från Exempel VVS AB via sms."
        )
        self.assertIn(
            "Utskick är aktiverat för Exempel VVS AB. Kunden har inte mejlats.",
            self.messages(response),
        )
        self.assertEqual(mail.outbox, [])
        self.assertTrue(access.is_enabled(account))

    def test_saving_without_the_box_creates_nothing(self):
        customer, account = self.new_customer()
        url = reverse("manage:utskick_customer_update", args=[customer.pk])
        self.staff_client().post(url, {"display_name": "Nykund"})
        self.assertFalse(UtskickSettings.objects.filter(account=account).exists())

    def test_disabling_keeps_the_contacts(self):
        make_contact(self.account, first_name="Anna", phone=PHONE_ANNA)
        url = reverse("manage:utskick_customer_update", args=[self.customer.pk])
        response = self.staff_client().post(url, {"display_name": "Exempelrör"})
        row = UtskickSettings.objects.get(pk=self.settings.pk)
        self.assertFalse(row.is_enabled)
        self.assertIsNotNone(row.disabled_at)
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 1)
        self.assertFalse(access.is_enabled(self.account))
        self.assertTrue(any("Kunden har inte mejlats" in m for m in self.messages(response)))
        self.assertEqual(mail.outbox, [])

    def test_the_slug_is_checked(self):
        url = reverse("manage:utskick_customer_update", args=[self.customer.pk])
        for slug in ("bekrafta", "annanfirma", "Inte giltig"):
            with self.subTest(slug=slug):
                self.staff_client().post(url, {"is_enabled": "on", "public_slug": slug})
                self.assertEqual(
                    UtskickSettings.objects.get(pk=self.settings.pk).public_slug, "exempelror"
                )
        self.staff_client().post(url, {"is_enabled": "on", "public_slug": "exempelror-nacka"})
        self.assertEqual(
            UtskickSettings.objects.get(pk=self.settings.pk).public_slug, "exempelror-nacka"
        )

    def test_stopping_all_sending_needs_a_reason(self):
        url = reverse("manage:utskick_customer_update", args=[self.customer.pk])
        self.staff_client().post(url, {"is_enabled": "on", "sending_blocked": "on"})
        self.assertFalse(UtskickSettings.objects.get(pk=self.settings.pk).sending_blocked)
        self.staff_client().post(
            url,
            {"is_enabled": "on", "sending_blocked": "on", "blocked_reason": "Klagomål på sms"},
        )
        row = UtskickSettings.objects.get(pk=self.settings.pk)
        self.assertTrue(row.sending_blocked)
        self.assertEqual(row.blocked_reason, "Klagomål på sms")

    def test_a_new_name_updates_only_the_standard_consent_texts(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(
            consent_text_email="Ja tack, mejla mig om Exempelrör."
        )
        url = reverse("manage:utskick_customer_update", args=[self.customer.pk])
        self.staff_client().post(url, {"is_enabled": "on", "display_name": "Exempelrör Nacka"})
        row = UtskickSettings.objects.get(pk=self.settings.pk)
        self.assertEqual(
            row.consent_text_sms, "Ja, jag vill få erbjudanden från Exempelrör Nacka via sms."
        )
        self.assertEqual(row.consent_text_email, "Ja tack, mejla mig om Exempelrör.")

    def test_limits_are_saved(self):
        url = reverse("manage:utskick_customer_update", args=[self.customer.pk])
        self.staff_client().post(
            url, {"is_enabled": "on", "contact_limit": "40000", "email_daily_cap": "500"}
        )
        row = UtskickSettings.objects.get(pk=self.settings.pk)
        self.assertEqual((row.contact_limit, row.email_daily_cap), (40000, 500))


class EndTests(ManageFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.anna_kontakt = make_contact(
            self.account, first_name="Anna", phone=PHONE_ANNA, email="anna@hemma.example"
        )
        consents.set_status(
            self.anna_kontakt,
            CHANNEL_SMS,
            consents.EXISTING,
            source=Consent.Source.MANUAL,
            evidence="Kund sedan 2020, Anna Andersson",
        )
        consents.set_status(
            self.anna_kontakt, CHANNEL_EMAIL, consents.UNSUBSCRIBED, source=Consent.Source.MANUAL
        )
        ContactList.objects.create(account=self.account, name="Kunder")
        Tag.objects.create(account=self.account, name="Nacka")
        SignupForm.objects.create(account=self.account, title="Anmälan")
        ImportJob.objects.create(
            account=self.account, original_name="kunder.csv", kind="csv", status="done"
        )
        self.other_kontakt = make_contact(self.other_account, first_name="Bo", phone=PHONE_BO)
        self.url = reverse("manage:utskick_customer_end", args=[self.customer.pk])

    def test_the_page_shows_what_goes(self):
        response = self.staff_client().get(self.url)
        self.assertContains(response, "1 kontakt med samtycken")
        self.assertContains(response, "1 lista, 1 tagg och 0 extrafält")
        self.assertContains(response, "Skriv kundens namn")

    def test_the_wrong_name_deletes_nothing(self):
        response = self.staff_client().post(self.url, {"namn": "Exempelrör"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Contact.objects.filter(pk=self.anna_kontakt.pk).exists())

    def test_the_right_name_deletes_everything_but_the_proof(self):
        response = self.staff_client().post(self.url, {"namn": " exempelrör ab "})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Contact.objects.filter(account=self.account).exists())
        self.assertFalse(ContactList.objects.filter(account=self.account).exists())
        self.assertFalse(Tag.objects.filter(account=self.account).exists())
        self.assertFalse(SignupForm.objects.filter(account=self.account).exists())
        self.assertFalse(ImportJob.objects.filter(account=self.account).exists())
        self.assertFalse(UtskickSettings.objects.get(pk=self.settings.pk).is_enabled)
        # Spärren och beviset finns kvar, utan kontakt och utan kundens anteckning.
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())
        logs = ConsentLog.objects.filter(account=self.account)
        self.assertTrue(logs.exists())
        self.assertFalse(logs.filter(contact__isnull=False).exists())
        self.assertFalse(logs.exclude(evidence="").exists())
        # Det andra kontot rörs inte.
        self.assertTrue(Contact.objects.filter(pk=self.other_kontakt.pk).exists())
        self.assertEqual(mail.outbox, [])


@override_settings(**AGENCY)
class SwitchTests(ManageFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("manage:utskick_switch")

    def row(self):
        return Switchboard.get_solo()

    def test_a_ready_mark_needs_a_note(self):
        response = self.staff_client().post(self.url, {"action": "ready", "field": "doi_ready_at"})
        self.assertIn(manage_views.NOTE_TEXT, self.messages(response))
        self.assertIsNone(self.row().doi_ready_at)
        self.staff_client().post(
            self.url,
            {"action": "ready", "field": "doi_ready_at", "note": "dkim=pass, provanmälan ok"},
        )
        row = self.row()
        self.assertIsNotNone(row.doi_ready_at)
        self.assertEqual(row.changed_by, self.staff)
        self.assertEqual(row.note, "dkim=pass, provanmälan ok")

    def test_an_unknown_field_is_refused(self):
        self.staff_client().post(
            self.url, {"action": "ready", "field": "hash_fingerprint", "note": "x"}
        )
        self.assertEqual(self.row().hash_fingerprint, "")

    def test_sms_needs_links_and_inbound(self):
        self.staff_client().post(self.url, {"action": "sms_on", "note": "klart"})
        self.assertFalse(self.row().sms_enabled)
        now = timezone.now()
        Switchboard.objects.filter(pk=1).update(links_ready_at=now, sms_inbound_ready_at=now)
        self.staff_client().post(self.url, {"action": "sms_on", "note": "klart"})
        self.assertTrue(self.row().sms_enabled)
        # Tas markeringen bort går brytaren av med den.
        self.staff_client().post(
            self.url, {"action": "unready", "field": "links_ready_at", "note": "cert gick ut"}
        )
        row = self.row()
        self.assertIsNone(row.links_ready_at)
        self.assertFalse(row.sms_enabled)

    def test_email_needs_ready_and_the_live_setting(self):
        Switchboard.get_solo()
        Switchboard.objects.filter(pk=1).update(email_ready_at=timezone.now())
        self.staff_client().post(self.url, {"action": "email_on", "note": "klart"})
        self.assertFalse(self.row().email_enabled)
        with override_settings(UTSKICK_EMAIL_LIVE=True):
            self.staff_client().post(self.url, {"action": "email_on", "note": "klart"})
        self.assertTrue(self.row().email_enabled)

    def test_the_emergency_stop_needs_no_note(self):
        Switchboard.get_solo()
        Switchboard.objects.filter(pk=1).update(
            sms_enabled=True, email_enabled=True, doi_ready_at=timezone.now()
        )
        response = self.staff_client().post(self.url, {"action": "stop_all"})
        row = self.row()
        self.assertFalse(row.sms_enabled)
        self.assertFalse(row.email_enabled)
        # Bekräftelsemejlen stoppas också (optin.due kräver markeringen).
        self.assertIsNone(row.doi_ready_at)
        self.assertIn(manage_views.STOP_ALL_TEXT, self.messages(response))
        self.assertEqual(mail.outbox, [])

    def test_the_switches_never_touch_the_fingerprints_or_the_heartbeat(self):
        keys.check_fingerprints()
        before = self.row()
        self.staff_client().post(self.url, {"action": "stop_all"})
        after = self.row()
        self.assertEqual(before.hash_fingerprint, after.hash_fingerprint)
        self.assertEqual(before.last_tick_at, after.last_tick_at)


class DpaPublishTests(ManageFixture, TestCase):
    BODY = (
        "<p>ADX Digitalbyrå är personuppgiftsbiträde och kunden är personuppgiftsansvarig "
        "för kontakterna i Kontakter.</p><p>Underbiträden: AWS i Stockholm och Irland, "
        "46elks i Sverige.</p>"
    )

    def page(self, published=True):
        page = BlockPage.objects.create(
            title="Biträdesavtal", slug="bitradesavtal", is_published=published
        )
        Block.objects.create(
            page=page, block_type="prose", order=1, data={"title": "Parter", "body": self.BODY}
        )
        Block.objects.create(
            page=page,
            block_type="steps",
            order=2,
            data={
                "title": "Radering",
                "steps": [{"title": "Vid uppsägning", "text": "Allt raderas när avtalet upphör."}],
            },
        )
        Block.objects.create(
            page=page, block_type="prose", order=3, is_visible=False, data={"body": "Dold"}
        )
        return page

    def publish(self, version="2026-11", confirm="1"):
        return self.staff_client().post(
            reverse("manage:utskick_dpa_publish"), {"version": version, "confirm": confirm}
        )

    def test_without_the_page_nothing_is_published(self):
        response = self.publish()
        self.assertTrue(any("finns inte" in m for m in self.messages(response)))
        self.assertEqual(DpaVersion.objects.filter(is_current=True).get(), self.dpa)

    def test_an_unpublished_page_is_refused(self):
        self.page(published=False)
        self.publish()
        self.assertEqual(DpaVersion.objects.filter(is_current=True).get(), self.dpa)

    def test_a_new_version_snapshots_the_text_and_becomes_current(self):
        self.page()
        self.publish()
        current = access.current_dpa()
        self.assertEqual(current.version, "2026-11")
        self.assertIn("personuppgiftsbiträde", current.text)
        self.assertIn("Vid uppsägning", current.text)
        self.assertIn("Allt raderas när avtalet upphör.", current.text)
        self.assertNotIn("Dold", current.text)
        self.assertNotIn("<p>", current.text)
        self.assertEqual(len(current.sha256), 64)
        self.assertEqual(current.published_by, self.staff)
        self.assertEqual(DpaVersion.objects.filter(is_current=True).count(), 1)
        # Kunden behöver godkänna den nya versionen för nya kontakter.
        self.assertFalse(access.dpa_ok(self.account))
        self.assertEqual(mail.outbox, [])

    def test_the_same_text_twice_is_refused(self):
        self.page()
        self.publish()
        response = self.publish(version="2026-12")
        self.assertTrue(any("densamma" in m for m in self.messages(response)))
        self.assertFalse(DpaVersion.objects.filter(version="2026-12").exists())

    def test_the_version_must_be_new_and_confirmed(self):
        self.page()
        self.publish(version="2026-10")
        self.publish(version="2026-11", confirm="")
        self.assertEqual(access.current_dpa(), self.dpa)


class OverviewTests(ManageFixture, TestCase):
    def test_the_overview_renders_for_the_agency(self):
        make_contact(self.account, first_name="Anna", phone=PHONE_ANNA)
        response = self.staff_client().get(reverse("manage:utskick_overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Exempelrör AB")
        self.assertContains(response, "Annanfirma AB")
        self.assertContains(response, 'id="nodstopp"')
        self.assertContains(response, 'id="avtal"')
        self.assertContains(response, "Ticken har aldrig gått")
        self.assertNotContains(response, PHONE_ANNA)

    def test_a_stale_tick_with_work_shows_a_warning(self):
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK, defaults={"doi_ready_at": timezone.now()}
        )
        kontakt = make_contact(self.account, first_name="Ella", email="ella@hemma.example")
        consents.set_status(kontakt, CHANNEL_EMAIL, consents.PENDING, source=Consent.Source.SIGNUP)
        with mock.patch("apps.utskick.optin.transport.can_send", return_value=True):
            response = self.staff_client().get(reverse("manage:utskick_overview"))
        self.assertContains(response, "Ticken går inte.")

    def test_the_queue_waits_for_the_ready_mark(self):
        kontakt = make_contact(self.account, first_name="Ella", email="ella@hemma.example")
        consents.set_status(kontakt, CHANNEL_EMAIL, consents.PENDING, source=Consent.Source.SIGNUP)
        with mock.patch("apps.utskick.optin.transport.can_send", return_value=True):
            response = self.staff_client().get(reverse("manage:utskick_overview"))
        self.assertContains(response, "väntar på klarmarkeringen")
        self.assertNotContains(response, "Ticken går inte.")

    def test_notes_are_marked_as_required(self):
        response = self.staff_client().get(reverse("manage:utskick_overview"))
        html = response.content.decode()
        for name in ("mu-sms-note", "mu-email-note", "mu-note-doi_ready_at"):
            tag = re.search(rf'<input id="{name}"[^>]*>', html).group(0)
            self.assertIn(" required", tag)
            self.assertIn('placeholder="Anteckning (krävs)"', tag)
        self.assertContains(response, "Stoppar också bekräftelsemejlen")

    def test_numbers_are_grouped(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(contact_limit=25000)
        response = self.staff_client().get(reverse("manage:utskick_overview"))
        self.assertContains(response, "0 av 25\u00a0000")

    def test_the_flamingo_overview_links_here(self):
        response = self.staff_client().get(reverse("manage:flamingo_overview"))
        self.assertContains(response, reverse("manage:utskick_overview"))
