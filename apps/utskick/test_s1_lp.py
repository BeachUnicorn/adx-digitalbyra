"""Kryssrutorna för utskick på landningssidorna (README C.2, H.5, J S1
test_s1_lp): exakta texter, aldrig förkryssade, borta när de inte får
visas, formulärets fel, beviset, kopplingen förfrågan till kontakt och
inga kakor.

Inget når nätet: inga sms (notify_new_lead skickar inget utan 46elks i
testerna) och inga mejl (bekräftelsemejlet skickas bara av ticken).
"""

import re
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import PageView
from apps.flamingo.models import Campaign, Fact, Lead, Service
from apps.flamingo.public_views import EMAIL_PENDING_TEXT, HONEYPOT
from apps.flamingo.testing import pages_from_campaigns

from . import capture
from . import consent as consents
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    ConsentLog,
    Contact,
    DpaVersion,
    Switchboard,
    UtskickSettings,
)
from .testing import UtskickFixture, make_contact

SMS_TEXT = "Ja, jag vill få erbjudanden från Exempelrör via sms."
EMAIL_TEXT = "Ja, jag vill få erbjudanden från Exempelrör via e-post."
MOBILE = "070-174 06 40"
MOBILE_E164 = "+46701740640"
#: Ingen ruta får ha checked, varken i en tom eller i en ifylld sida.
CHECKED = re.compile(r'<input[^>]*name="consent_[a-z]+"[^>]*\schecked')


class LpFixture(UtskickFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key, label, value in (
            ("telefon", "Telefon", "08-465 004 00"),
            ("orgnr", "Organisationsnummer", "559999-0000"),
        ):
            Fact.objects.create(
                account=cls.account, key=key, label=label, value=value, confirmed=True
            )
        cls.jour = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL
        )
        cls.badrum = Service.objects.create(
            account=cls.account, name="Badrumsrenovering", sales_mode=Service.SALES_QUOTE
        )
        cls.call_page = Campaign.objects.create(
            account=cls.account,
            service=cls.jour,
            name="Rörjour Nacka",
            status=Campaign.STATUS_LIVE,
            page={"title": "Rörjour i Nacka", "phone": "08-465 004 00", "questions": []},
        )
        cls.quote_page = Campaign.objects.create(
            account=cls.account,
            service=cls.badrum,
            name="Badrum Nacka",
            status=Campaign.STATUS_LIVE,
            page={
                "title": "Badrumsrenovering i Nacka",
                "phone": "08-465 004 00",
                "form_title": "Berätta om ditt badrum",
                "questions": [{"key": "storlek", "label": "Hur stort?", "kind": "text"}],
            },
        )
        cls.draft = Campaign.objects.create(
            account=cls.account, service=cls.badrum, name="Utkast", page={"title": "Utkast"}
        )
        pages_from_campaigns(cls.call_page, cls.quote_page, cls.draft)

    def doi_ready(self):
        Switchboard.get_solo()
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(doi_ready_at=timezone.now())

    def page(self, campaign):
        response = Client().get(campaign.landing_url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def call_post(self, **extra):
        data = {"name": "Sara Holm", "phone": MOBILE}
        data.update(extra)
        return Client().post(self.call_page.landing_url, data)

    def quote_post(self, **extra):
        data = {
            "name": "Maria Nilsson",
            "phone": MOBILE,
            "email": "maria@hemma.example",
            "q_storlek": "6",
        }
        data.update(extra)
        return Client().post(self.quote_page.landing_url, data)


class CheckboxTests(LpFixture, TestCase):
    def test_the_sms_box_has_the_exact_text_and_is_not_checked(self):
        html = self.page(self.call_page)
        self.assertIn('name="consent_sms"', html)
        self.assertIn(f">{SMS_TEXT}</label>", html)
        self.assertNotRegex(html, CHECKED)
        self.assertNotIn('name="consent_email"', html)

    def test_email_needs_an_email_field_and_doi_ready(self):
        html = self.page(self.quote_page)
        self.assertIn('name="consent_sms"', html)
        self.assertNotIn('name="consent_email"', html)
        self.doi_ready()
        html = self.page(self.quote_page)
        self.assertIn('name="consent_email"', html)
        self.assertIn(f">{EMAIL_TEXT}</label>", html)
        self.assertNotRegex(html, CHECKED)
        # Det korta formuläret frågar inte efter e-post: ingen ruta för e-post.
        self.assertNotIn('name="consent_email"', self.page(self.call_page))

    def test_the_privacy_link_sits_under_the_boxes(self):
        html = self.page(self.call_page)
        url = reverse("utskick_public:privacy", args=["exempelror"])
        self.assertIn(f'href="{url}"', html)
        self.assertIn("Så hanterar Exempelrör dina uppgifter", html)
        self.assertLess(html.index('name="consent_sms"'), html.index("Så hanterar Exempelrör"))

    def test_the_customers_own_policy_is_linked_when_set(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(
            privacy_url="https://exempelror.example/integritet/"
        )
        Fact.objects.filter(account=self.account, key="orgnr").delete()
        html = self.page(self.call_page)
        self.assertIn('href="https://exempelror.example/integritet/"', html)

    def test_absent_when_the_customer_turned_them_off(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(lp_consent=False)
        self.assertNotIn("consent_", self.page(self.call_page))

    def test_absent_when_utskick_is_off(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        self.assertNotIn("consent_", self.page(self.call_page))

    def test_absent_without_a_current_dpa(self):
        DpaVersion.objects.filter(pk=self.dpa.pk).update(is_current=False)
        DpaVersion.objects.create(version="2026-11", text="Ny", sha256="1" * 64, is_current=True)
        self.assertNotIn("consent_", self.page(self.call_page))

    def test_absent_without_a_privacy_notice(self):
        Fact.objects.filter(account=self.account, key="orgnr").delete()
        self.assertEqual(capture.privacy_url(self.account), "")
        self.assertNotIn("consent_", self.page(self.call_page))

    def test_absent_for_an_account_without_utskick(self):
        html = Client().get(self.call_page.landing_url).content.decode()
        self.assertIn("consent_sms", html)
        UtskickSettings.objects.filter(pk=self.settings.pk).delete()
        self.assertNotIn("consent_", self.page(self.call_page))

    def test_the_visitors_own_tick_stays_after_an_error(self):
        self.doi_ready()
        response = self.quote_post(email="", consent_email="1")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertRegex(html, r'name="consent_email"[^>]*\schecked')
        self.assertNotRegex(html, r'name="consent_sms"[^>]*\schecked')


class FieldErrorTests(LpFixture, TestCase):
    def test_email_box_without_an_email(self):
        self.doi_ready()
        response = self.quote_post(email="", consent_email="1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, capture.EMAIL_NEEDED_TEXT)
        self.assertFalse(Lead.objects.filter(account=self.account).exists())
        self.assertFalse(Contact.objects.exists())

    def test_sms_box_with_a_landline(self):
        response = self.call_post(phone="08-465 004 01", consent_sms="1")
        self.assertEqual(response.status_code, 200)
        # Felet säger vad besökaren kan göra, och rutan märks också.
        self.assertEqual(
            capture.NOT_MOBILE_TEXT,
            "Det här numret kan inte få sms. Skriv ett mobilnummer, eller kryssa ur rutan för sms.",
        )
        self.assertContains(response, capture.NOT_MOBILE_TEXT)
        html = response.content.decode()
        self.assertIn('class="rn-check is-error"', html)
        self.assertIn('aria-describedby="rn-phone-fel"', html)
        self.assertIn('id="rn-phone-fel"', html)
        self.assertFalse(Lead.objects.filter(account=self.account).exists())
        # Utan kryss går samma förfrågan fram.
        self.assertEqual(self.call_post(phone="08-465 004 01").status_code, 302)

    def test_a_landline_without_the_box_is_a_normal_lead(self):
        response = self.call_post(phone="08-465 004 01")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Lead.objects.filter(account=self.account).exists())
        self.assertFalse(Contact.objects.exists())


class CaptureTests(LpFixture, TestCase):
    def test_a_ticked_sms_box_stores_the_proof_and_links_the_lead(self):
        response = self.call_post(consent_sms="1")
        thanks = reverse("flamingo_public:thanks", args=[self.call_page.page_slug])
        self.assertRedirects(response, thanks, fetch_redirect_response=False)
        lead = Lead.objects.get(account=self.account)
        kontakt = Contact.objects.get(account=self.account)
        self.assertEqual(lead.contact, kontakt)
        self.assertEqual(kontakt.phone, MOBILE_E164)
        self.assertEqual(kontakt.source, Contact.Source.FORM)
        row = kontakt.consents.get(channel=CHANNEL_SMS)
        self.assertEqual(row.status, Consent.Status.YES)
        self.assertEqual(row.text_shown, SMS_TEXT)
        self.assertEqual(row.source, Consent.Source.LP_FORM)
        self.assertEqual(row.source_detail, self.call_page.landing_url)
        log = ConsentLog.objects.filter(contact=kontakt, new_status="yes").get()
        self.assertEqual(log.text_shown, SMS_TEXT)
        self.assertTrue(log.ip_hash)
        self.assertEqual(log.by_label, "Personen själv")

    def test_a_ticked_email_box_waits_for_the_confirmation(self):
        self.doi_ready()
        response = self.quote_post(consent_email="1")
        thanks = reverse("flamingo_public:thanks", args=[self.quote_page.page_slug])
        self.assertRedirects(response, thanks + "?epost=1", fetch_redirect_response=False)
        kontakt = Contact.objects.get(account=self.account)
        email = kontakt.consents.get(channel=CHANNEL_EMAIL)
        self.assertEqual(email.status, Consent.Status.PENDING)
        self.assertEqual(email.text_shown, EMAIL_TEXT)
        self.assertIsNone(email.confirm_sent_at)
        # Sms-rutan var inte ikryssad: inget samtycke för sms.
        self.assertEqual(kontakt.consents.get(channel=CHANNEL_SMS).status, "missing")
        page = Client().get(thanks + "?epost=1").content.decode()
        self.assertIn(EMAIL_PENDING_TEXT, page)
        self.assertNotIn(EMAIL_PENDING_TEXT, Client().get(thanks).content.decode())

    def test_the_thanks_note_follows_the_box_not_the_outcome(self):
        """Granskningen, säkerhet 3: ?epost=1 när rutan var ikryssad, också
        när adressen redan får e-post (sidan avslöjar inte vem som finns)."""
        self.doi_ready()
        kontakt = make_contact(self.account, first_name="Maria", email="maria@hemma.example")
        consents.set_status(
            kontakt, CHANNEL_EMAIL, consents.YES, source=Consent.Source.MANUAL, evidence="kassan"
        )
        response = self.quote_post(consent_email="1")
        thanks = reverse("flamingo_public:thanks", args=[self.quote_page.page_slug])
        self.assertRedirects(response, thanks + "?epost=1", fetch_redirect_response=False)
        self.assertEqual(kontakt.consents.get(channel=CHANNEL_EMAIL).status, Consent.Status.YES)
        self.assertNotIn("Vi har skickat", EMAIL_PENDING_TEXT)

    def test_a_plain_lead_creates_no_contact(self):
        self.call_post()
        lead = Lead.objects.get(account=self.account)
        self.assertIsNone(lead.contact)
        self.assertFalse(Contact.objects.exists())

    def test_a_plain_lead_is_linked_only_on_an_exact_match(self):
        kontakt = make_contact(self.account, first_name="Sara", phone=MOBILE_E164)
        consents_before = list(kontakt.consents.values_list("status", flat=True))
        self.call_post()
        lead = Lead.objects.get(account=self.account)
        self.assertEqual(lead.contact, kontakt)
        self.assertEqual(list(kontakt.consents.values_list("status", flat=True)), consents_before)
        self.call_post(phone="070-174 06 41")
        other = Lead.objects.filter(account=self.account).exclude(pk=lead.pk).get()
        self.assertIsNone(other.contact)

    def test_another_accounts_contact_is_never_linked(self):
        make_contact(self.other_account, first_name="Sara", phone=MOBILE_E164)
        self.call_post()
        self.assertIsNone(Lead.objects.get(account=self.account).contact)
        self.call_post(consent_sms="1")
        lead = Lead.objects.filter(account=self.account, contact__isnull=False).get()
        self.assertEqual(lead.contact.account_id, self.account.pk)

    def test_nothing_without_can_collect(self):
        make_contact(self.account, first_name="Sara", phone=MOBILE_E164)
        UtskickSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        # Rutan syns inte, och en postad ruta gör ingenting.
        self.call_post(consent_sms="1")
        self.assertIsNone(Lead.objects.get(account=self.account).contact)
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 1)

    def test_preview_and_honeypot_create_nothing(self):
        staff = Client()
        staff.force_login(self.staff)
        staff.post(self.draft.landing_url, {"name": "Sara", "phone": MOBILE, "consent_sms": "1"})
        self.call_post(consent_sms="1", **{HONEYPOT: "http://spam.example"})
        self.assertFalse(Lead.objects.filter(account=self.account).exists())
        self.assertFalse(Contact.objects.exists())

    def test_a_failure_in_utskick_never_loses_the_lead(self):
        with mock.patch("apps.utskick.contacts.create", side_effect=RuntimeError("pang")):
            response = self.call_post(consent_sms="1")
        self.assertEqual(response.status_code, 302)
        lead = Lead.objects.get(account=self.account)
        self.assertIsNone(lead.contact)

    def test_even_an_unexpected_error_in_capture_keeps_the_lead(self):
        with (
            mock.patch("apps.utskick.capture.from_lead_form", side_effect=RuntimeError("pang")),
            self.assertLogs("security", level="ERROR"),
        ):
            response = self.call_post(consent_sms="1")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Lead.objects.filter(account=self.account).exists())

    def test_deleting_the_contact_keeps_the_lead(self):
        self.call_post(consent_sms="1")
        Contact.objects.filter(account=self.account).delete()
        lead = Lead.objects.get(account=self.account)
        self.assertIsNone(lead.contact)


class NoCookieTests(LpFixture, TestCase):
    def test_the_page_with_boxes_gets_no_adx_cookies(self):
        self.doi_ready()
        response = Client().get(self.quote_page.landing_url, HTTP_USER_AGENT="Mozilla/5.0")
        self.assertIn("consent_email", response.content.decode())
        self.assertEqual(sorted(set(response.cookies) - {"csrftoken"}), [])
        self.assertFalse(PageView.objects.exists())

    def test_the_thanks_page_gets_no_cookies(self):
        thanks = reverse("flamingo_public:thanks", args=[self.quote_page.page_slug])
        response = Client().get(thanks + "?epost=1", HTTP_USER_AGENT="Mozilla/5.0")
        self.assertEqual(sorted(set(response.cookies) - {"csrftoken"}), [])
        self.assertFalse(PageView.objects.exists())
