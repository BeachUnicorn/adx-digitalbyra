"""
Tester för de publika sidorna och bekräftelsemejlet (README J, S1,
test_s1_public.py): anmälan, tack, integritet, bekräfta e-post, Mina
utskick, optin (dubbel opt-in), transporten, tokens och capture (kontakter
från landningssidans formulär).

Inget når nätet: SES är FakeSes, och testkörningen stänger av
UTSKICK_EMAIL_LIVE (config/test_runner.py).
"""

import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail as django_mail
from django.core import signing
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import Fact, FlamingoAccount, Lead
from apps.projects.models import Customer

from . import capture, consent, limits, optin, tokens
from . import suppression as suppressions
from .email import mime, transport
from .keys import value_hash
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    ConsentLog,
    Contact,
    ContactList,
    Counter,
    DpaAcceptance,
    Event,
    ListMembership,
    SignupForm,
    Suppression,
    Switchboard,
    Tag,
    UtskickSettings,
)
from .testing import PHONE_ANNA, PHONE_BO, UtskickFixture, make_contact

User = get_user_model()

EMAIL = "lisa@kund.example"
FIRST_NAME = "Mohammed"


def aged_token(seconds_ago=10):
    """En giltig botskyddstoken utfärdad bakåt i tiden (människotempo)."""
    return signing.dumps({"t": int(time.time()) - seconds_ago}, salt="botcheck")


def signup_post(**overrides):
    token = aged_token()
    data = {
        "first_name": "",
        "email": EMAIL,
        "consent_email": "1",
        "bc_website": "",
        "bc_time": token,
        "bc_proof": token,
    }
    data.update(overrides)
    return data


class PublicFixture(UtskickFixture):
    """Exempelrör med en påslagen anmälningssida, uppgifter för den
    genererade integritetssidan och klarmarkerade bekräftelsemejl."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Fact.objects.create(
            account=cls.account,
            key="orgnr",
            label="Organisationsnummer",
            value="556677-8899",
            confirmed=True,
        )
        Fact.objects.create(
            account=cls.account,
            key="epost",
            label="E-post",
            value="hej@exempelror.example",
            confirmed=True,
        )
        cls.signup_form = SignupForm.objects.create(
            account=cls.account, title="Få våra erbjudanden", intro="Påminnelser och erbjudanden."
        )
        SignupForm.objects.filter(pk=cls.signup_form.pk).update(is_active=True)
        Switchboard.objects.create(pk=Switchboard.SOLO_PK, doi_ready_at=timezone.now())

    def setUp(self):
        super().setUp()
        self.client = Client()

    @property
    def signup_url(self):
        return reverse("utskick_public:signup", args=["exempelror"])

    @property
    def thanks_url(self):
        return reverse("utskick_public:signup_thanks", args=["exempelror"])

    def sign_up(self, **overrides):
        return self.client.post(self.signup_url, signup_post(**overrides))

    def email_consent(self, address=EMAIL, account=None):
        return Consent.objects.get(
            contact__account=account or self.account, contact__email=address, channel=CHANNEL_EMAIL
        )


# ---------------------------------------------------------------------------
# Anmälningssidan
# ---------------------------------------------------------------------------


class SignupPageTests(PublicFixture, TestCase):
    def test_page_shows_the_exact_consent_text_unchecked_with_botcheck(self):
        response = self.client.get(self.signup_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Ja, jag vill få erbjudanden från Exempelrör via e-post.", html)
        self.assertNotIn("checked", html)
        self.assertIn("data-botcheck=", html)
        self.assertIn('name="bc_proof" value=""', html)
        self.assertIn('name="bc_website"', html)
        self.assertIn("js/utskick-public.js", html)
        self.assertIn("Så hanterar Exempelrör dina uppgifter", html)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        # S1 erbjuder bara e-post (README L, Security 13).
        self.assertNotIn('name="consent_sms"', html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("style=", html)

    def test_404_when_the_form_is_inactive(self):
        SignupForm.objects.filter(pk=self.signup_form.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.signup_url).status_code, 404)
        self.assertEqual(self.sign_up().status_code, 404)

    def test_404_without_can_collect(self):
        DpaAcceptance.objects.filter(account=self.account).delete()
        self.assertEqual(self.client.get(self.signup_url).status_code, 404)
        self.assertEqual(self.sign_up().status_code, 404)
        self.assertFalse(Contact.objects.filter(account=self.account).exists())

    def test_404_when_utskick_is_off(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        self.assertEqual(self.client.get(self.signup_url).status_code, 404)

    def test_404_without_a_privacy_notice(self):
        Fact.objects.filter(account=self.account, key="orgnr").delete()
        self.assertEqual(self.client.get(self.signup_url).status_code, 404)
        UtskickSettings.objects.filter(pk=self.settings.pk).update(
            privacy_url="https://exempelror.example/integritet"
        )
        self.assertEqual(self.client.get(self.signup_url).status_code, 200)

    def test_404_for_an_unknown_slug(self):
        url = reverse("utskick_public:signup", args=["finns-inte"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_closed_until_doi_is_ready_except_for_the_agency(self):
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(doi_ready_at=None)
        self.assertEqual(self.client.get(self.signup_url).status_code, 404)
        staff = Client()
        staff.force_login(self.staff)
        response = staff.get(self.signup_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Förhandsvisning för ADX.")

    def test_real_signup_creates_a_pending_contact_with_proof(self):
        response = self.sign_up(first_name=FIRST_NAME, email=" Lisa@Kund.Example ")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(self.thanks_url + "?r="))
        contact = Contact.objects.get(account=self.account, email=EMAIL)
        self.assertEqual(contact.first_name, FIRST_NAME)
        self.assertEqual(contact.source, Contact.Source.SIGNUP)
        row = self.email_consent()
        self.assertEqual(row.status, consent.PENDING)
        self.assertEqual(row.source, Consent.Source.SIGNUP)
        self.assertEqual(row.text_shown, "Ja, jag vill få erbjudanden från Exempelrör via e-post.")
        self.assertEqual(row.source_detail, "/utskick/exempelror/")
        self.assertIsNone(row.confirm_sent_at)
        log = ConsentLog.objects.filter(contact=contact, new_status=consent.PENDING).get()
        self.assertTrue(log.ip_hash)
        self.assertEqual(log.by_label, "Personen själv")
        self.assertTrue(Event.objects.filter(contact=contact, kind=Event.SIGNUP).exists())

    def test_thanks_page_shows_the_masked_address(self):
        response = self.sign_up()
        page = self.client.get(response["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertContains(
            page, "Om l***@k***.example inte redan får erbjudanden från Exempelrör får du ett mejl"
        )
        self.assertNotContains(page, EMAIL)

    def test_thanks_page_never_tells_who_is_already_a_subscriber(self):
        """Granskningen, säkerhet 3: samma sida för en ny adress och för en
        som redan får erbjudanden (masken är densamma: l***@k***.example)."""
        known = make_contact(self.account, email="lars@kund.example")
        consent.set_status(
            known, CHANNEL_EMAIL, consent.YES, source=Consent.Source.MANUAL, evidence="kassan"
        )
        pages = []
        for address in ("lars@kund.example", EMAIL):
            self.client = Client()
            response = self.sign_up(email=address)
            self.assertEqual(response.status_code, 302)
            pages.append(self.client.get(response["Location"]).content)
        self.assertEqual(pages[0], pages[1])
        self.assertNotIn("får redan erbjudanden från Exempelrör via".encode(), pages[0])

    def test_a_signup_for_a_known_address_changes_nothing_on_the_contact(self):
        known = make_contact(self.account, email=EMAIL)
        consent.set_status(
            known, CHANNEL_EMAIL, consent.YES, source=Consent.Source.MANUAL, evidence="kassan"
        )
        kunder = ContactList.objects.create(account=self.account, name="Kunder")
        SignupForm.objects.filter(pk=self.signup_form.pk).update(add_to_list=kunder)
        self.sign_up()
        self.assertFalse(ListMembership.objects.filter(contact=known).exists())
        self.assertFalse(Event.objects.filter(contact=known, kind=Event.SIGNUP).exists())

    @override_settings(INQUIRY_NOTIFICATION_EMAIL="byran@adx.example")
    def test_at_most_a_hundred_signups_per_account_and_hour(self):
        from .public_views import SIGNUP_PER_ACCOUNT_HOUR

        Counter.objects.create(
            scope="signup_account",
            key=str(self.account.pk),
            window=limits.hour_window(),
            count=SIGNUP_PER_ACCOUNT_HOUR,
        )
        response = self.sign_up()
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "många anmälningar just nu", status_code=429)
        self.assertFalse(Contact.objects.filter(email=EMAIL).exists())
        self.sign_up()
        self.assertEqual(len(django_mail.outbox), 1, "ett larm till byrån per timme")
        self.assertNotIn(EMAIL, django_mail.outbox[0].body)

    def test_thanks_without_reference_is_generic(self):
        page = self.client.get(self.thanks_url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "***")
        forged = self.client.get(self.thanks_url + "?r=1.abc.zzzzzzzzzzzz")
        self.assertNotContains(forged, "***")

    def test_thanks_reference_of_another_account_shows_nothing(self):
        other = make_contact(self.other_account, email="olle@annan.example")
        row = Consent.objects.get(contact=other, channel=CHANNEL_EMAIL)
        url = f"{self.thanks_url}?r={tokens.thanks_token(row.pk)}"
        self.assertNotContains(self.client.get(url), "o***@a***.example")

    def test_account_comes_only_from_the_slug(self):
        response = self.client.post(
            self.signup_url + f"?account={self.other_account.pk}",
            signup_post(account=str(self.other_account.pk)),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Contact.objects.filter(account=self.account, email=EMAIL).exists())
        self.assertFalse(Contact.objects.filter(account=self.other_account).exists())
        # Annanfirma har ingen anmälningssida: 404, inget skapas.
        other = reverse("utskick_public:signup", args=["annanfirma"])
        self.assertEqual(self.client.post(other, signup_post()).status_code, 404)

    def test_the_list_and_tags_of_the_form_come_with_the_confirmation(self):
        kunder = ContactList.objects.create(account=self.account, name="Kunder")
        tag = Tag.objects.create(account=self.account, name="Webb")
        foreign = Tag.objects.create(account=self.other_account, name="Annan")
        SignupForm.objects.filter(pk=self.signup_form.pk).update(add_to_list=kunder)
        self.signup_form.add_tags.add(tag, foreign)
        self.sign_up()
        contact = Contact.objects.get(account=self.account, email=EMAIL)
        # Obekräftat: vem som helst kan skriva en adress.
        self.assertFalse(ListMembership.objects.filter(contact=contact).exists())
        self.assertFalse(contact.tags.exists())
        row = self.email_consent()
        self.client.post(reverse("utskick_public:confirm", args=[tokens.doi_token(row)]))
        self.assertEqual(self.email_consent().status, consent.YES)
        membership = ListMembership.objects.get(list=kunder, contact=contact)
        self.assertEqual(membership.source, ListMembership.Source.SIGNUP)
        self.assertEqual(list(contact.tags.values_list("name", flat=True)), ["Webb"])

    def test_existing_contact_keeps_name_and_gets_pending(self):
        contact = make_contact(self.account, first_name="Lisa", email=EMAIL)
        self.sign_up(first_name="Någon")
        contact.refresh_from_db()
        self.assertEqual(contact.first_name, "Lisa")
        self.assertEqual(self.email_consent().status, consent.PENDING)
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 1)

    def test_signup_over_yes_changes_nothing(self):
        contact = make_contact(self.account, email=EMAIL)
        consent.set_status(
            contact,
            CHANNEL_EMAIL,
            consent.YES,
            source=Consent.Source.MANUAL,
            evidence="kassan, 2024",
        )
        response = self.sign_up()
        page = self.client.get(response["Location"])
        self.assertContains(page, "inte redan får erbjudanden från Exempelrör")
        self.assertEqual(self.email_consent().status, consent.YES)

    def test_signup_on_a_suppressed_address_waits_for_confirmation(self):
        suppressions.add(self.account, CHANNEL_EMAIL, value_hash(CHANNEL_EMAIL, EMAIL), "link")
        self.sign_up()
        row = self.email_consent()
        self.assertEqual(row.status, consent.PENDING)
        self.assertTrue(
            suppressions.is_suppressed(self.account, CHANNEL_EMAIL, value=EMAIL),
            "spärren står kvar tills personen klickat i mejlet",
        )


class SignupValidationTests(PublicFixture, TestCase):
    def test_box_must_be_ticked(self):
        response = self.sign_up(consent_email="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kryssa i rutan för att anmäla dig.")
        self.assertFalse(Contact.objects.exists())

    def test_email_is_required_for_email(self):
        response = self.sign_up(email="")
        self.assertContains(response, "Fyll i din e-post för e-post.")
        self.assertFalse(Contact.objects.exists())

    def test_invalid_email(self):
        response = self.sign_up(email="inte-en-adress")
        self.assertContains(response, "E-postadressen är inte giltig.")

    def test_first_name_rules(self):
        for bad in ("Anna2", "<b>Anna</b>", "x" * 41, "Anna; DROP", "@anna"):
            with self.subTest(name=bad):
                response = self.sign_up(first_name=bad)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Skriv bara ditt förnamn, med bokstäver.")
        self.assertFalse(Contact.objects.exists())
        for good in ("Anna-Lena", "O'Brien", "Åsa Maria", ""):
            with self.subTest(name=good):
                response = self.sign_up(first_name=good, email=f"x{len(good)}@kund.example")
                self.assertEqual(response.status_code, 302)

    def test_box_stays_ticked_after_an_error(self):
        response = self.sign_up(email="")
        self.assertContains(response, "checked")

    def test_errors_and_help_are_tied_to_the_fields(self):
        html = self.sign_up(email="inte-en-adress").content.decode()
        self.assertIn('aria-invalid="true" aria-describedby="up-email-fel"', html)
        self.assertIn('<div id="up-email-fel">', html)
        self.assertIn('aria-describedby="up-consent-help"', html)
        self.assertIn('id="up-consent-help"', html)
        html = self.sign_up(consent_email="").content.decode()
        self.assertIn('aria-describedby="up-consent-help up-consent-fel"', html)


class SignupBotTests(PublicFixture, TestCase):
    def test_missing_js_proof_gives_silent_fake_success(self):
        response = self.sign_up(bc_proof="")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.thanks_url)
        self.assertFalse(Contact.objects.exists())
        self.assertFalse(Consent.objects.exists())

    def test_honeypot_gives_silent_fake_success(self):
        response = self.sign_up(bc_website="https://spam.example")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.thanks_url)
        self.assertFalse(Contact.objects.exists())

    def test_too_fast_gives_silent_fake_success(self):
        fresh = signing.dumps({"t": int(time.time())}, salt="botcheck")
        response = self.sign_up(bc_time=fresh)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Contact.objects.exists())

    def test_the_js_fills_the_proof_from_the_form(self):
        script = (Path(settings.BASE_DIR) / "static" / "js" / "utskick-public.js").read_text()
        self.assertIn("form[data-botcheck]", script)
        self.assertIn('input[name="bc_proof"]', script)
        self.assertIn("form.dataset.botcheck", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("cookie", script)

    def test_five_signups_per_ip_and_hour(self):
        from .public_views import SIGNUP_PER_IP_HOUR

        self.assertEqual(SIGNUP_PER_IP_HOUR, 5)
        for n in range(SIGNUP_PER_IP_HOUR):
            response = self.sign_up(email=f"person{n}@kund.example")
            self.assertEqual(response.status_code, 302, n)
        response = self.sign_up(email="person9@kund.example")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "många anmälningar", status_code=429)
        self.assertFalse(Contact.objects.filter(email="person9@kund.example").exists())
        # Ett annat konto räknas för sig.
        SignupForm.objects.create(account=self.other_account, title="Anmälan", is_active=True)
        Fact.objects.create(
            account=self.other_account,
            key="orgnr",
            label="Organisationsnummer",
            value="556000-0001",
            confirmed=True,
        )
        Fact.objects.create(
            account=self.other_account,
            key="telefon",
            label="Telefon",
            value="08-1234567",
            confirmed=True,
        )
        url = reverse("utskick_public:signup", args=["annanfirma"])
        self.assertEqual(self.client.post(url, signup_post()).status_code, 302)

    def test_contact_limit_shows_a_calm_message(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(contact_limit=1)
        make_contact(self.account, first_name="Bo", phone=PHONE_BO)
        response = self.sign_up()
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "Det går inte att anmäla sig här just nu.", status_code=503)


class NoCookiesTests(PublicFixture, TestCase):
    def test_no_analytics_cookies_on_public_pages(self):
        contact = make_contact(self.account, email=EMAIL)
        row = Consent.objects.get(contact=contact, channel=CHANNEL_EMAIL)
        pages = [
            self.signup_url,
            self.thanks_url,
            reverse("utskick_public:privacy", args=["exempelror"]),
            reverse(
                "utskick_public:preferences",
                args=[tokens.preference_token(self.account.pk, CHANNEL_EMAIL, row.value_hash)],
            ),
        ]
        for url in pages:
            with self.subTest(url=url):
                response = Client().get(url, HTTP_USER_AGENT="Mozilla/5.0 (iPhone)")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    sorted(set(response.cookies) - {"csrftoken"}), [], "inga kakor utom CSRF"
                )
                self.assertNotIn("analytics", response.content.decode())


# ---------------------------------------------------------------------------
# Bekräftelsemejlet (optin) och transporten
# ---------------------------------------------------------------------------


class DoiMailTests(PublicFixture, TestCase):
    def test_tick_sends_one_doi_without_submitted_text(self):
        self.sign_up(first_name=FIRST_NAME)
        with transport.FakeSes() as ses:
            summary = optin.send_due()
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(len(ses.calls), 1)
        call = ses.calls[0]
        self.assertEqual(call["FromEmailAddress"], "bekrafta@utskick.adx.se")
        self.assertEqual(call["Destination"], {"ToAddresses": [EMAIL]})
        message = ses.messages[0]
        self.assertEqual(message["From"].addresses[0].display_name, "Exempelrör")
        self.assertEqual(message["From"].addresses[0].addr_spec, "bekrafta@utskick.adx.se")
        self.assertIsNone(message["Reply-To"])
        self.assertEqual(message["Subject"], "Bekräfta att du vill få e-post från Exempelrör")
        text = mime.text_part(message)
        html = mime.text_part(message, "html")
        for body in (text, html):
            self.assertNotIn(FIRST_NAME, body)
            self.assertNotIn(EMAIL, body)
            self.assertIn("/utskick/bekrafta/", body)
            self.assertIn("/utskick/exempelror/integritet/", body)
            self.assertIn("Exempelrör", body)
        row = self.email_consent()
        self.assertIsNotNone(row.confirm_sent_at)
        self.assertEqual(row.confirm_count, 1)
        with transport.FakeSes() as ses:
            self.assertEqual(optin.send_due()["sent"], 0)
        self.assertEqual(ses.calls, [])

    def test_nothing_is_sent_without_transport(self):
        self.sign_up()
        self.assertFalse(transport.can_send())
        self.assertFalse(optin.work_exists())
        self.assertEqual(optin.send_due()["sent"], 0)
        self.assertIsNone(self.email_consent().confirm_sent_at)

    def test_demo_never_sends(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.sign_up()
        with transport.FakeSes() as ses:
            optin.send_due()
        self.assertEqual(ses.calls, [])

    def test_disabled_account_never_sends(self):
        self.sign_up()
        UtskickSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        with transport.FakeSes() as ses:
            self.assertFalse(optin.work_exists())
            optin.send_due()
        self.assertEqual(ses.calls, [])

    def test_a_customer_with_sending_stopped_gets_no_doi(self):
        """D.2: kundens "Stoppa all sändning för kunden" gäller också
        bekräftelsemejlen (granskningen, säkerhet 1)."""
        self.sign_up()
        UtskickSettings.objects.filter(pk=self.settings.pk).update(
            sending_blocked=True, blocked_reason="Klagomål"
        )
        with transport.FakeSes() as ses:
            self.assertFalse(optin.work_exists())
            self.assertEqual(optin.send_due()["sent"], 0)
        self.assertEqual(ses.calls, [])
        self.assertIsNone(self.email_consent().confirm_sent_at, "står kvar i kön")
        UtskickSettings.objects.filter(pk=self.settings.pk).update(sending_blocked=False)
        with transport.FakeSes() as ses:
            self.assertEqual(optin.send_due()["sent"], 1)

    def test_without_the_ready_mark_nothing_is_sent(self):
        self.sign_up()
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(doi_ready_at=None)
        with transport.FakeSes() as ses:
            self.assertFalse(optin.work_exists())
            self.assertEqual(optin.send_due()["sent"], 0)
        self.assertEqual(ses.calls, [])
        self.assertEqual(optin.queued().count(), 1, "raden väntar i kön")

    def test_the_emergency_stop_stops_doi_mails(self):
        self.sign_up()
        staff = Client()
        staff.force_login(self.staff)
        staff.post(reverse("manage:utskick_switch"), {"action": "stop_all"})
        self.assertIsNone(Switchboard.get_solo().doi_ready_at)
        with transport.FakeSes() as ses:
            self.assertFalse(optin.work_exists())
            self.assertEqual(optin.send_due()["sent"], 0)
        self.assertEqual(ses.calls, [])

    def test_the_agency_test_signup_is_sent_before_the_ready_mark(self):
        """Checklistans steg 6: byråns provanmälan går ut före markeringen,
        ingen annans."""
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(doi_ready_at=None)
        staff = Client()
        staff.force_login(self.staff)
        staff.post(self.signup_url, signup_post(email="prov@adx.example"))
        self.client.post(self.signup_url, signup_post())  # 404 för alla andra
        self.assertFalse(Contact.objects.filter(email=EMAIL).exists())
        row = self.email_consent("prov@adx.example")
        self.assertEqual(row.changed_by, self.staff)
        log = ConsentLog.objects.get(contact=row.contact, new_status=consent.PENDING)
        self.assertTrue(log.by_staff)
        # En väntande rad från någon annan (till exempel landningssidan) väntar.
        other = make_contact(self.account, email="annan@kund.example")
        consent.set_status(other, CHANNEL_EMAIL, consent.PENDING, source=Consent.Source.LP_FORM)
        with transport.FakeSes() as ses:
            self.assertEqual(optin.send_due()["sent"], 1)
        self.assertEqual(ses.calls[0]["Destination"], {"ToAddresses": ["prov@adx.example"]})

    def test_at_most_a_hundred_doi_per_account_and_hour(self):
        self.sign_up()
        Counter.objects.create(
            scope="optin_account_hour",
            key=str(self.account.pk),
            window=limits.hour_window(),
            count=optin.PER_ACCOUNT_HOUR,
        )
        with override_settings(INQUIRY_NOTIFICATION_EMAIL="byran@adx.example"):
            with transport.FakeSes() as ses:
                summary = optin.send_due()
                optin.send_due()
        self.assertEqual(ses.calls, [])
        self.assertEqual(summary["waiting"], 1)
        self.assertIsNone(self.email_consent().confirm_sent_at, "står kvar i kön")
        self.assertEqual(len(django_mail.outbox), 1, "ett larm till byrån per timme")
        self.assertEqual(django_mail.outbox[0].to, ["byran@adx.example"])
        self.assertNotIn(EMAIL, django_mail.outbox[0].body)

    def test_one_doi_per_address_and_account_per_day(self):
        self.sign_up()
        with transport.FakeSes() as ses:
            optin.send_due()
            self.client = Client()
            self.sign_up()
            optin.send_due()
        self.assertEqual(len(ses.calls), 1)

    def test_signing_up_again_another_day_sends_again(self):
        self.sign_up()
        with transport.FakeSes() as ses:
            optin.send_due()
            row = self.email_consent()
            tomorrow = timezone.now() + timedelta(days=1)
            self.assertTrue(optin.requeue(row, now=tomorrow))
            optin.send_due(now=tomorrow)
        self.assertEqual(len(ses.calls), 2)

    def test_three_doi_per_address_across_adx(self):
        self.sign_up()
        Counter.objects.create(
            scope="optin_addr_all",
            key=value_hash(CHANNEL_EMAIL, EMAIL),
            window=limits.day_window(),
            count=optin.ADDR_ALL_DAY,
        )
        with transport.FakeSes() as ses:
            summary = optin.send_due()
        self.assertEqual(ses.calls, [])
        self.assertEqual(summary["skipped"], 1)

    def test_hourly_cap_across_adx(self):
        self.sign_up()
        Counter.objects.create(
            scope="optin_hour", key="", window=limits.hour_window(), count=optin.PER_HOUR
        )
        with transport.FakeSes() as ses:
            summary = optin.send_due()
        self.assertEqual(ses.calls, [])
        self.assertEqual(summary["waiting"], 1)
        self.assertIsNone(self.email_consent().confirm_sent_at, "står kvar i kön")

    def test_throttling_leaves_the_row_in_the_queue(self):
        self.sign_up()
        with transport.FakeSes(fail="TooManyRequestsException", status=429) as ses:
            summary = optin.send_due()
        self.assertEqual(len(ses.calls), 1)
        self.assertEqual(summary["waiting"], 1)
        self.assertIsNone(self.email_consent().confirm_sent_at)

    def test_rejected_mail_is_not_retried(self):
        self.sign_up()
        with transport.FakeSes(fail="MessageRejected") as ses:
            summary = optin.send_due()
            optin.send_due()
        self.assertEqual(len(ses.calls), 1)
        self.assertEqual(summary["failed"], 1)
        row = self.email_consent()
        self.assertIsNotNone(row.confirm_sent_at)
        self.assertEqual(row.confirm_count, 0)

    def test_without_current_dpa_no_new_confirmations_are_sent(self):
        self.sign_up()
        DpaAcceptance.objects.filter(account=self.account).delete()
        with transport.FakeSes() as ses:
            summary = optin.send_due()
        self.assertEqual(ses.calls, [])
        self.assertEqual(summary["skipped"], 1)

    @override_settings(DEBUG=True)
    def test_debug_writes_an_eml_file_instead(self):
        import tempfile

        self.sign_up()
        with tempfile.TemporaryDirectory() as folder, override_settings(PRIVATE_MEDIA_ROOT=folder):
            self.assertEqual(optin.send_due()["sent"], 1)
            files = list((Path(folder) / transport.EML_FOLDER).glob("*.eml"))
            self.assertEqual(len(files), 1)
            message = mime.parse(files[0].read_bytes())
            self.assertIn("Bekräfta", message["Subject"])

    def test_off_in_production_writes_nothing(self):
        mail = transport.OutgoingMail(
            to=EMAIL,
            from_name="Exempelrör",
            from_addr="bekrafta@utskick.adx.se",
            subject="x",
            text="x",
        )
        result = transport.send(mail, kind=transport.DOI)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "email_off")

    def test_unknown_kind_is_refused(self):
        mail = transport.OutgoingMail(
            to=EMAIL, from_name="", from_addr="a@b.example", subject="", text=""
        )
        with self.assertRaises(ValueError):
            transport.send(mail, kind="utskick")

    def test_header_injection_is_flattened(self):
        mail = transport.OutgoingMail(
            to=EMAIL,
            from_name="Exempelrör\r\nBcc: x@evil.example",
            from_addr="bekrafta@utskick.adx.se",
            subject="Hej\nBcc: y@evil.example",
            text="x",
        )
        message = mime.parse(mime.build(mail))
        self.assertIsNone(message["Bcc"])


# ---------------------------------------------------------------------------
# Bekräftelsesidan
# ---------------------------------------------------------------------------


class ConfirmPageTests(PublicFixture, TestCase):
    def pending(self, **kwargs):
        self.sign_up(**kwargs)
        return self.email_consent()

    def url_for(self, row, now=None):
        return reverse("utskick_public:confirm", args=[tokens.doi_token(row, now)])

    def test_get_does_not_confirm_post_does(self):
        row = self.pending()
        url = self.url_for(row)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<span class="up-nowrap">e-post</span> från Exempelrör')
        self.assertContains(response, "Ja, bekräfta")
        self.assertContains(response, "l***@k***.example")
        self.assertNotContains(response, EMAIL)
        self.assertEqual(response["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(self.email_consent().status, consent.PENDING)

        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        row = self.email_consent()
        self.assertEqual(row.status, consent.YES)
        self.assertEqual(row.source, Consent.Source.DOI)
        self.assertIsNotNone(row.confirmed_at)
        self.assertEqual(row.text_shown, "Ja, jag vill få erbjudanden från Exempelrör via e-post.")
        self.assertEqual(row.source_detail, "/utskick/exempelror/")
        log = ConsentLog.objects.filter(contact=row.contact, new_status=consent.YES).get()
        self.assertEqual(log.source, Consent.Source.DOI)
        self.assertTrue(log.ip_hash)
        self.assertTrue(consent.eligible(row.contact, CHANNEL_EMAIL, consent.REKLAM))

        done = self.client.get(url)
        self.assertContains(done, "Klart")
        self.assertContains(done, "/utskick/val/")

    def test_confirm_lifts_a_suppression(self):
        suppressions.add(self.account, CHANNEL_EMAIL, value_hash(CHANNEL_EMAIL, EMAIL), "link")
        row = self.pending()
        self.client.post(self.url_for(row))
        self.assertEqual(self.email_consent().status, consent.YES)
        self.assertFalse(suppressions.is_suppressed(self.account, CHANNEL_EMAIL, value=EMAIL))

    def test_expired_link_does_not_confirm(self):
        row = self.pending()
        old = timezone.now() - timedelta(days=tokens.DOI_DAYS + 1)
        url = self.url_for(row, now=old)
        self.assertEqual(self.client.get(url).status_code, 410)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "Länken har gått ut", status_code=410)
        self.assertContains(response, "/utskick/exempelror/", status_code=410)
        self.assertEqual(self.email_consent().status, consent.PENDING)

    def test_last_valid_day_still_works(self):
        row = self.pending()
        old = timezone.now() - timedelta(days=tokens.DOI_DAYS - 1)
        self.client.post(self.url_for(row, now=old))
        self.assertEqual(self.email_consent().status, consent.YES)

    def test_tampered_token_is_404(self):
        row = self.pending()
        token = tokens.doi_token(row)
        bad = token[:-1] + ("A" if token[-1] != "A" else "B")
        url = reverse("utskick_public:confirm", args=[bad])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_link_dies_when_the_address_changes(self):
        from . import contacts

        row = self.pending()
        url = self.url_for(row)
        contacts.change_address(row.contact, CHANNEL_EMAIL, "ny@kund.example")
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_declined_after_signup_is_not_confirmed(self):
        row = self.pending()
        consent.set_status(
            row.contact, CHANNEL_EMAIL, consent.DECLINED, source=Consent.Source.PREFERENCE
        )
        response = self.client.post(self.url_for(row))
        self.assertEqual(response.status_code, 410)
        self.assertEqual(self.email_consent().status, consent.DECLINED)

    def test_no_confirmation_without_current_dpa(self):
        row = self.pending()
        DpaAcceptance.objects.filter(account=self.account).delete()
        response = self.client.post(self.url_for(row))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "tar inte emot nya anmälningar")
        self.assertEqual(self.email_consent().status, consent.PENDING)

    def test_confirm_uses_csrf(self):
        row = self.pending()
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(self.url_for(row)).status_code, 403)
        self.assertEqual(self.email_consent().status, consent.PENDING)


# ---------------------------------------------------------------------------
# Mina utskick
# ---------------------------------------------------------------------------


class PreferencePageTests(PublicFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.kontakt = make_contact(self.account, first_name="Anna", phone=PHONE_ANNA, email=EMAIL)
        for channel in (CHANNEL_SMS, CHANNEL_EMAIL):
            consent.set_status(
                self.kontakt,
                channel,
                consent.YES,
                source=Consent.Source.MANUAL,
                evidence="kassan, 2024",
            )
        self.url = reverse(
            "utskick_public:preferences",
            args=[
                tokens.preference_token(
                    self.account.pk, CHANNEL_EMAIL, value_hash(CHANNEL_EMAIL, EMAIL)
                )
            ],
        )

    def status(self, channel):
        return Consent.objects.get(contact=self.kontakt, channel=channel).status

    def post(self, **data):
        token = aged_token()
        payload = {"bc_website": "", "bc_time": token, "bc_proof": token, "action": "spara"}
        payload.update(data)
        return self.client.post(self.url, payload)

    def test_page_shows_masked_details_and_rows(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vad vill du få från Exempelrör?")
        self.assertContains(response, "070-*** ** 01")
        self.assertContains(response, "l***@k***.example")
        self.assertNotContains(response, "Anna")
        self.assertNotContains(response, EMAIL)
        self.assertContains(response, "Sms med erbjudanden")
        self.assertContains(response, "E-post med erbjudanden")
        self.assertContains(response, "Information om dina bokningar skickas")
        self.assertContains(response, "Avregistrera mig från allt")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_get_changes_nothing(self):
        self.client.get(self.url)
        self.assertEqual(self.status(CHANNEL_EMAIL), consent.YES)

    def test_toggle_off_sets_declined_and_information_continues(self):
        response = self.post(sms="1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.status(CHANNEL_EMAIL), consent.DECLINED)
        self.assertEqual(self.status(CHANNEL_SMS), consent.YES)
        self.assertFalse(Suppression.objects.exists())
        self.kontakt.refresh_from_db()
        self.assertTrue(consent.eligible(self.kontakt, CHANNEL_EMAIL, consent.INFORMATION))
        self.assertFalse(consent.eligible(self.kontakt, CHANNEL_EMAIL, consent.REKLAM))
        page = self.client.get(response["Location"])
        self.assertContains(page, "Dina val är sparade.")

    def test_toggle_on_needs_a_confirmation(self):
        self.post(sms="1")
        response = self.post(sms="1", email="1")
        self.assertIn("klart=mejl", response["Location"])
        row = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_EMAIL)
        self.assertEqual(row.status, consent.PENDING)
        self.assertEqual(row.source, Consent.Source.PREFERENCE)
        self.assertIsNone(row.confirm_sent_at)
        page = self.client.get(response["Location"])
        self.assertContains(page, "Vi har skickat ett mejl till l***@k***.example.")
        with transport.FakeSes() as ses:
            optin.send_due()
        self.assertEqual(len(ses.calls), 1)

    def test_sms_cannot_be_turned_on_here_in_s1(self):
        self.post(email="1")
        self.assertEqual(self.status(CHANNEL_SMS), consent.DECLINED)
        self.post(sms="1", email="1")
        self.assertEqual(self.status(CHANNEL_SMS), consent.DECLINED)
        page = self.client.get(self.url)
        self.assertNotContains(page, 'name="sms"')

    def test_turning_on_needs_the_botcheck_but_turning_off_does_not(self):
        self.post(sms="1")
        self.post(sms="1", email="1", bc_proof="")
        self.assertEqual(self.status(CHANNEL_EMAIL), consent.DECLINED)
        self.post(email="1", bc_proof="")
        self.assertEqual(self.status(CHANNEL_SMS), consent.DECLINED)

    def test_the_token_never_reaches_the_log(self):
        """Granskningen, säkerhet 5: länken går aldrig ut och får inte stå i
        journalen. Botskyddet prövas bara när en kanal slås på, och då
        loggas sökvägen tvättad."""
        token = self.url.rstrip("/").rsplit("/", 1)[1]
        with self.assertNoLogs("security", level="WARNING"):
            # En avstängning (e-post av) prövar inget botskydd.
            self.post(sms="1", bc_proof="", bc_time="")
        self.assertEqual(self.status(CHANNEL_EMAIL), consent.DECLINED)
        with self.assertLogs("security", level="WARNING") as logs:
            self.client.post(self.url, {"action": "spara", "sms": "1", "email": "1"})
        self.assertTrue(logs.output)
        self.assertNotIn(token, "\n".join(logs.output))
        self.assertIn("/utskick/val/", "\n".join(logs.output))

    def test_rows_that_are_off_say_why(self):
        self.post(action="allt")
        page = self.client.get(self.url)
        self.assertContains(page, "Du har avregistrerat dig.")
        kontakt = make_contact(self.account, phone=PHONE_BO, email="bo@kund.example")
        url = reverse(
            "utskick_public:preferences",
            args=[
                tokens.preference_token(
                    self.account.pk, CHANNEL_EMAIL, value_hash(CHANNEL_EMAIL, kontakt.email)
                )
            ],
        )
        page = self.client.get(url)
        self.assertContains(page, "Går inte att slå på här.")

    def test_unsubscribe_from_everything_suppresses_both_channels(self):
        response = self.post(action="allt")
        self.assertIn("klart=avregistrerad", response["Location"])
        self.assertEqual(self.status(CHANNEL_SMS), consent.UNSUBSCRIBED)
        self.assertEqual(self.status(CHANNEL_EMAIL), consent.UNSUBSCRIBED)
        reasons = set(
            Suppression.objects.filter(account=self.account).values_list("reason", flat=True)
        )
        self.assertEqual(reasons, {Suppression.Reason.PREFERENCE})
        self.assertEqual(Suppression.objects.filter(account=self.account).count(), 2)
        self.kontakt.refresh_from_db()
        self.assertFalse(consent.eligible(self.kontakt, CHANNEL_EMAIL, consent.INFORMATION))
        page = self.client.get(response["Location"])
        self.assertContains(page, "Du får inga fler utskick från Exempelrör.")
        self.assertNotContains(page, "Information om dina bokningar skickas")

    def test_unsubscribe_shows_the_customer_text(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(
            unsubscribe_text="Ring oss om du vill boka ändå."
        )
        response = self.post(action="allt")
        self.assertContains(self.client.get(response["Location"]), "Ring oss om du vill boka ändå.")

    def test_opt_out_works_when_utskick_is_off(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.post(sms="1")
        self.assertEqual(self.status(CHANNEL_EMAIL), consent.DECLINED)
        self.post(action="allt")
        self.assertEqual(self.status(CHANNEL_SMS), consent.UNSUBSCRIBED)

    def test_turning_on_needs_can_collect(self):
        self.post(sms="1")
        DpaAcceptance.objects.filter(account=self.account).delete()
        self.post(sms="1", email="1")
        self.assertEqual(self.status(CHANNEL_EMAIL), consent.DECLINED)

    def test_resubscribe_after_unsubscribe_goes_through_doi(self):
        self.post(action="allt")
        self.post(email="1")
        row = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_EMAIL)
        self.assertEqual(row.status, consent.PENDING)
        self.assertTrue(suppressions.is_suppressed(self.account, CHANNEL_EMAIL, value=EMAIL))
        self.client.post(reverse("utskick_public:confirm", args=[tokens.doi_token(row)]))
        self.assertEqual(self.status(CHANNEL_EMAIL), consent.YES)
        self.assertFalse(suppressions.is_suppressed(self.account, CHANNEL_EMAIL, value=EMAIL))

    def test_link_for_a_deleted_contact_still_unsubscribes(self):
        from . import contacts

        contacts.delete_contact(self.kontakt, suppress=False)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "E-post med erbjudanden")
        self.post(action="allt")
        self.assertTrue(suppressions.is_suppressed(self.account, CHANNEL_EMAIL, value=EMAIL))

    def test_tampered_or_foreign_tokens_are_404(self):
        token = self.url.rstrip("/").rsplit("/", 1)[1]
        account36, channel, hash43, sig = token.split(".")
        other36 = tokens._b36(self.other_account.pk)
        forged = f"{other36}.{channel}.{hash43}.{sig}"
        url = reverse("utskick_public:preferences", args=[forged])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"action": "allt"}).status_code, 404)
        self.assertFalse(Suppression.objects.exists())

    def test_preferences_use_csrf(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(self.url, {"action": "allt"}).status_code, 403)
        self.assertFalse(Suppression.objects.exists())


# ---------------------------------------------------------------------------
# Integritetssidan
# ---------------------------------------------------------------------------


class PrivacyPageTests(PublicFixture, TestCase):
    @property
    def url(self):
        return reverse("utskick_public:privacy", args=["exempelror"])

    def test_generated_page_names_company_org_number_and_processor(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for text in (
            "Så hanterar Exempelrör dina uppgifter",
            "556677-8899",
            "hej@exempelror.example",
            "personuppgiftsbiträde",
            "13 månader",
            "25 månader",
            "36 månader",
            "Amazon Web Services",
            "46elks",
            "Integritetsskyddsmyndigheten",
        ):
            self.assertContains(response, text)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        # Landningssidorna sätter CSRF-kakan: texten lovar inte "inga kakor".
        self.assertNotContains(response, "sätter inga kakor")
        self.assertContains(response, "sätter ingen kaka för spårning")

    def test_404_without_org_number_or_contact_details(self):
        Fact.objects.filter(account=self.account, key="epost").delete()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_unconfirmed_facts_do_not_count(self):
        Fact.objects.filter(account=self.account, key="orgnr").update(confirmed=False)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_own_policy_redirects(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(
            privacy_url="https://exempelror.example/integritet"
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://exempelror.example/integritet")

    def test_still_there_when_utskick_is_off(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_privacy_url_helper(self):
        self.assertEqual(capture.privacy_url(self.account), "/utskick/exempelror/integritet/")
        self.assertTrue(
            capture.privacy_url(self.account, absolute=True).endswith(
                "/utskick/exempelror/integritet/"
            )
        )
        UtskickSettings.objects.filter(pk=self.settings.pk).update(
            privacy_url="http://inte-https.example/"
        )
        Fact.objects.filter(account=self.account).delete()
        self.assertEqual(capture.privacy_url(self.account), "")


# ---------------------------------------------------------------------------
# Signerade länkar
# ---------------------------------------------------------------------------


class TokenTests(PublicFixture, TestCase):
    def test_preference_round_trip_and_tamper(self):
        digest = value_hash(CHANNEL_EMAIL, EMAIL)
        token = tokens.preference_token(self.account.pk, CHANNEL_EMAIL, digest)
        self.assertEqual(
            tokens.read_preference(token),
            tokens.PreferenceRef(self.account.pk, CHANNEL_EMAIL, digest),
        )
        parts = token.split(".")
        self.assertEqual(len(parts[2]), 43)
        self.assertEqual(len(parts[3]), 16)
        for bad in (
            token + "x",
            token.replace(".email.", ".sms."),
            "",
            "a.b.c",
            "...",
            "zz.email.!!!.aaaaaaaaaaaaaaaa",
        ):
            with self.subTest(token=bad):
                self.assertIsNone(tokens.read_preference(bad))

    def test_kinds_do_not_mix(self):
        contact = make_contact(self.account, email=EMAIL)
        row = Consent.objects.get(contact=contact, channel=CHANNEL_EMAIL)
        doi = tokens.doi_token(row)
        self.assertIsNone(tokens.read_preference(doi))
        self.assertIsNone(tokens.read_thanks(doi))

    def test_signature_uses_the_link_key(self):
        digest = value_hash(CHANNEL_EMAIL, EMAIL)
        token = tokens.preference_token(self.account.pk, CHANNEL_EMAIL, digest)
        with override_settings(UTSKICK_LINK_KEY="nyckeln-byttes"):
            self.assertIsNone(tokens.read_preference(token))
        with override_settings(SECRET_KEY="secret-byttes-utan-betydelse"):
            self.assertIsNotNone(tokens.read_preference(token))

    def test_thanks_token_expires(self):
        token = tokens.thanks_token(42, now=timezone.now() - timedelta(hours=2))
        self.assertIsNone(tokens.read_thanks(token))
        self.assertEqual(tokens.read_thanks(tokens.thanks_token(42)), 42)


# ---------------------------------------------------------------------------
# Landningssidans formulär (capture)
# ---------------------------------------------------------------------------

TEXT_SMS = "Ja, jag vill få erbjudanden från Exempelrör via sms."
TEXT_EMAIL = "Ja, jag vill få erbjudanden från Exempelrör via e-post."
TEXTS = {CHANNEL_SMS: TEXT_SMS, CHANNEL_EMAIL: TEXT_EMAIL}


class CaptureTests(PublicFixture, TestCase):
    def lead(self, account=None, **fields):
        data = {"name": "Lisa Berg", "phone": "070-174 06 04", "email": EMAIL}
        data.update(fields)
        return Lead.objects.create(account=account or self.account, **data)

    def test_channels_need_collect_lp_consent_and_privacy(self):
        spec = SimpleNamespace(asks_email=True)
        self.assertEqual(capture.lp_consent_channels(self.account, spec), ["sms", "email"])
        self.assertEqual(
            capture.lp_consent_channels(self.account, SimpleNamespace(asks_email=False)), ["sms"]
        )
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(doi_ready_at=None)
        self.assertEqual(capture.lp_consent_channels(self.account, spec), ["sms"])
        UtskickSettings.objects.filter(pk=self.settings.pk).update(lp_consent=False)
        self.assertEqual(capture.lp_consent_channels(self.account, spec), [])
        UtskickSettings.objects.filter(pk=self.settings.pk).update(lp_consent=True)
        Fact.objects.filter(account=self.account, key="orgnr").delete()
        self.assertEqual(capture.lp_consent_channels(self.account, spec), [])
        self.assertEqual(
            capture.lp_consent_channels(self.other_account, spec), [], "inga uppgifter"
        )

    def test_channels_need_a_current_dpa(self):
        DpaAcceptance.objects.filter(account=self.account).delete()
        self.assertEqual(
            capture.lp_consent_channels(self.account, SimpleNamespace(asks_email=True)), []
        )

    def test_consent_errors(self):
        channels = ["sms", "email"]
        self.assertEqual(
            capture.consent_errors(
                channels, {"consent_email": True, "email": "", "consent_sms": False}
            ),
            {"email": capture.EMAIL_NEEDED_TEXT},
        )
        self.assertEqual(
            capture.consent_errors(channels, {"consent_sms": True, "phone": "08-123 456 78"}),
            {"phone": capture.NOT_MOBILE_TEXT},
        )
        self.assertEqual(
            capture.consent_errors(channels, {"consent_sms": True, "phone": "070-174 06 04"}),
            {},
        )
        self.assertEqual(capture.consent_errors([], {"consent_email": True, "email": ""}), {})

    def test_plain_lead_creates_no_contact(self):
        lead = self.lead()
        result = capture.from_lead_form(lead, {}, TEXTS, "/lp/varmepump/", "h" * 64)
        self.assertIsNone(result.contact)
        self.assertFalse(Contact.objects.exists())
        lead.refresh_from_db()
        self.assertIsNone(lead.contact_id)

    def test_plain_lead_links_an_exact_match(self):
        existing = make_contact(self.account, first_name="Lisa", email=EMAIL)
        lead = self.lead(phone="")
        result = capture.from_lead_form(lead, {}, TEXTS, "/lp/varmepump/")
        self.assertEqual(result.contact, existing)
        lead.refresh_from_db()
        self.assertEqual(lead.contact_id, existing.pk)
        self.assertEqual(
            Consent.objects.get(contact=existing, channel=CHANNEL_EMAIL).status, "missing"
        )

    def test_sms_box_gives_yes_with_proof(self):
        lead = self.lead()
        result = capture.from_lead_form(
            lead, {"consent_sms": True}, TEXTS, "/lp/varmepump/", "h" * 64
        )
        contact = result.contact
        self.assertEqual(contact.phone, "+46701740604")
        self.assertEqual(contact.first_name, "Lisa")
        self.assertEqual(contact.source, Contact.Source.FORM)
        row = Consent.objects.get(contact=contact, channel=CHANNEL_SMS)
        self.assertEqual(row.status, consent.YES)
        self.assertEqual(row.text_shown, TEXT_SMS)
        self.assertEqual(row.source, Consent.Source.LP_FORM)
        self.assertEqual(row.source_detail, "/lp/varmepump/")
        self.assertEqual(
            Consent.objects.get(contact=contact, channel=CHANNEL_EMAIL).status, "missing"
        )
        self.assertEqual(
            ConsentLog.objects.get(contact=contact, new_status="yes").ip_hash, "h" * 64
        )
        lead.refresh_from_db()
        self.assertEqual(lead.contact_id, contact.pk)
        self.assertFalse(result.email_pending)

    def test_email_box_waits_for_doi(self):
        lead = self.lead()
        result = capture.from_lead_form(lead, {"consent_email": True}, TEXTS, "/lp/varmepump/")
        self.assertTrue(result.email_pending)
        row = Consent.objects.get(contact=result.contact, channel=CHANNEL_EMAIL)
        self.assertEqual(row.status, consent.PENDING)
        self.assertEqual(row.text_shown, TEXT_EMAIL)
        with transport.FakeSes() as ses:
            optin.send_due()
        self.assertEqual(len(ses.calls), 1)

    def test_box_without_a_shown_text_counts_for_nothing(self):
        lead = self.lead()
        result = capture.from_lead_form(
            lead, {"consent_email": True}, {CHANNEL_SMS: TEXT_SMS}, "/lp/varmepump/"
        )
        self.assertIsNone(result.contact)
        self.assertFalse(Contact.objects.exists())

    def test_nothing_without_can_collect(self):
        DpaAcceptance.objects.filter(account=self.account).delete()
        lead = self.lead()
        result = capture.from_lead_form(
            lead, {"consent_sms": True, "consent_email": True}, TEXTS, "/lp/varmepump/"
        )
        self.assertIsNone(result.contact)
        self.assertFalse(Contact.objects.exists())
        lead.refresh_from_db()
        self.assertIsNone(lead.contact_id)

    def test_nothing_when_utskick_is_off(self):
        customer = Customer.objects.create(name="Avstängd AB")
        account = FlamingoAccount.objects.create(customer=customer, is_enabled=True)
        lead = self.lead(account=account)
        result = capture.from_lead_form(lead, {"consent_sms": True}, TEXTS, "/lp/x/")
        self.assertIsNone(result.contact)
        self.assertFalse(Contact.objects.exists())

    def test_existing_address_is_never_overwritten(self):
        existing = make_contact(self.account, first_name="Lisa", phone=PHONE_BO, email=EMAIL)
        lead = self.lead()
        result = capture.from_lead_form(
            lead, {"consent_sms": True, "consent_email": True}, TEXTS, "/lp/varmepump/"
        )
        existing.refresh_from_db()
        self.assertEqual(existing.phone, PHONE_BO)
        # Numret i formuläret är inte kontaktens: inget sms-samtycke sparas,
        # och förfrågan kopplas inte (adresserna säger emot varandra).
        self.assertIsNone(result.contact)
        lead.refresh_from_db()
        self.assertIsNone(lead.contact_id)
        self.assertEqual(
            Consent.objects.get(contact=existing, channel=CHANNEL_SMS).status, "missing"
        )
        self.assertEqual(
            Consent.objects.get(contact=existing, channel=CHANNEL_EMAIL).status, consent.PENDING
        )

    def test_a_form_never_adds_an_address_to_an_existing_contact(self):
        """Granskningen, säkerhet 2: den som känner till Veras nummer skriver
        det och sin egen e-post och kryssar i e-post. Veras kontakt får ingen
        e-post, inget mejl går till främlingen och inget namn skrivs."""
        vera = make_contact(self.account, phone="+46701740604")
        lead = self.lead(name="Främling Fransson", email="framling@annan.example")
        result = capture.from_lead_form(
            lead, {"consent_email": True}, TEXTS, "/lp/varmepump/", "h" * 64
        )
        vera.refresh_from_db()
        self.assertEqual(vera.email, "")
        self.assertEqual(vera.first_name, "")
        self.assertFalse(result.email_pending)
        self.assertFalse(Consent.objects.filter(contact=vera, channel=CHANNEL_EMAIL).exists())
        self.assertFalse(ConsentLog.objects.filter(contact=vera).exclude(new_status="missing"))
        self.assertFalse(optin.queued().exists())
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 1)

    def test_a_form_never_adds_a_number_to_an_existing_contact(self):
        existing = make_contact(self.account, first_name="Lisa", email=EMAIL)
        lead = self.lead()
        result = capture.from_lead_form(lead, {"consent_sms": True}, TEXTS, "/lp/varmepump/")
        existing.refresh_from_db()
        self.assertEqual(existing.phone, "")
        self.assertFalse(Consent.objects.filter(contact=existing, channel=CHANNEL_SMS).exists())
        # E-posten är kontaktens och inget säger emot: förfrågan kopplas.
        self.assertEqual(result.contact, existing)

    def test_suppressed_number_stays_suppressed(self):
        suppressions.add(self.account, CHANNEL_SMS, value_hash(CHANNEL_SMS, "+46701740604"), "stop")
        lead = self.lead()
        result = capture.from_lead_form(lead, {"consent_sms": True}, TEXTS, "/lp/varmepump/")
        row = Consent.objects.get(contact=result.contact, channel=CHANNEL_SMS)
        self.assertEqual(row.status, consent.UNSUBSCRIBED)

    def test_other_accounts_contacts_are_never_touched(self):
        other = make_contact(self.other_account, email=EMAIL)
        lead = self.lead(phone="")
        result = capture.from_lead_form(lead, {}, TEXTS, "/lp/varmepump/")
        self.assertIsNone(result.contact)
        lead.refresh_from_db()
        self.assertIsNone(lead.contact_id)
        self.assertNotEqual(lead.contact_id, other.pk)

    def test_tracking_sentence_sets_tracking_ok(self):
        text = TEXT_EMAIL + " " + capture.TRACKING_SENTENCE
        lead = self.lead()
        result = capture.from_lead_form(
            lead, {"consent_email": True}, {CHANNEL_EMAIL: text}, "/lp/varmepump/"
        )
        row = Consent.objects.get(contact=result.contact, channel=CHANNEL_EMAIL)
        self.assertTrue(row.tracking_ok)


# ---------------------------------------------------------------------------
# Inställningarna för anmälningssidan (Kontakter > Anmälan)
# ---------------------------------------------------------------------------


class SignupSettingsTests(UtskickFixture, TestCase):
    url = "/flamingo/app/kontakter/anmalan/"

    def setUp(self):
        super().setUp()
        self.anna_client = self.client_for(self.anna)
        Fact.objects.create(
            account=self.account,
            key="orgnr",
            label="Organisationsnummer",
            value="556677-8899",
            confirmed=True,
        )
        Fact.objects.create(
            account=self.account,
            key="epost",
            label="E-post",
            value="hej@exempelror.example",
            confirmed=True,
        )

    def test_url_name(self):
        self.assertEqual(reverse("flamingo:app_signup"), self.url)

    def test_page_explains_why_it_is_closed(self):
        response = self.anna_client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sidan är stängd för besökarna.")
        self.assertContains(response, "Bekräftelsemejlen är inte påslagna än. Be ADX slå på dem.")
        self.assertContains(response, "Sidan är avstängd.")
        self.assertContains(response, "/utskick/exempelror/")
        # Förhandsgranska finns också för en stängd sida (I.7).
        self.assertContains(response, "/utskick/exempelror/?forhandsgranska=1")
        self.assertContains(response, "De läggs i listan och får taggarna när de har klickat")
        self.assertNotContains(response, "style=")
        self.assertFalse(SignupForm.objects.exists(), "GET skapar ingen sida")

    def test_the_customer_previews_a_closed_page_but_cannot_send_it(self):
        SignupForm.objects.create(account=self.account, title="Få våra erbjudanden")
        url = reverse("utskick_public:signup", args=["exempelror"])
        preview = url + "?forhandsgranska=1"
        self.assertEqual(Client().get(url).status_code, 404)
        self.assertEqual(Client().get(preview).status_code, 404, "inte för främlingar")
        other = User.objects.create_user("olle@annan.example", password="x")
        self.other_customer.users.add(other)
        stranger = Client()
        stranger.force_login(other)
        self.assertEqual(stranger.get(preview).status_code, 404, "inte för en annan kund")
        response = self.anna_client.get(preview)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Förhandsgranskning.")
        self.assertContains(response, "Få våra erbjudanden")
        self.assertContains(response, " disabled>Anmäl mig</button>")
        self.assertEqual(response["Cache-Control"], "private, no-store, max-age=0")
        self.assertNotContains(response, "style=")
        staff = Client()
        staff.force_login(self.staff)
        self.assertEqual(staff.get(preview).status_code, 200)
        # Förhandsgranskningen tar inte emot något.
        response = self.anna_client.post(preview, signup_post())
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Contact.objects.filter(email=EMAIL).exists())

    def test_save_creates_the_page_with_list_and_tags(self):
        kunder = ContactList.objects.create(account=self.account, name="Kunder")
        tag = Tag.objects.create(account=self.account, name="Webb")
        response = self.anna_client.post(
            self.url,
            {
                "title": "Få våra erbjudanden",
                "intro": "Påminnelser och erbjudanden.",
                "add_to_list": str(kunder.pk),
                "add_tags": [str(tag.pk)],
                "is_active": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        sida = SignupForm.objects.get(account=self.account)
        self.assertTrue(sida.is_active)
        self.assertEqual(sida.channels, [CHANNEL_EMAIL])
        self.assertEqual(sida.add_to_list, kunder)
        self.assertEqual(list(sida.add_tags.all()), [tag])
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK, defaults={"doi_ready_at": timezone.now()}
        )
        self.assertEqual(
            Client().get(reverse("utskick_public:signup", args=["exempelror"])).status_code, 200
        )
        page = self.anna_client.get(self.url)
        self.assertContains(page, "Sidan är öppen.")
        self.assertContains(page, "Förhandsgranska")

    def test_foreign_list_or_tag_gives_400(self):
        foreign_list = ContactList.objects.create(account=self.other_account, name="Annan")
        foreign_tag = Tag.objects.create(account=self.other_account, name="Annan")
        for data in ({"add_to_list": str(foreign_list.pk)}, {"add_tags": [str(foreign_tag.pk)]}):
            with self.subTest(data=data):
                response = self.anna_client.post(self.url, {"title": "Anmälan", **data})
                self.assertEqual(response.status_code, 400)
        self.assertFalse(SignupForm.objects.exists())

    def test_title_is_required_and_cleaned(self):
        response = self.anna_client.post(self.url, {"title": "  "})
        self.assertContains(response, "Skriv en rubrik.")
        self.anna_client.post(self.url, {"title": "Hej " + chr(0x2013) + " erbjudanden"})
        self.assertEqual(SignupForm.objects.get().title, "Hej - erbjudanden")
        self.assertFalse(SignupForm.objects.get().is_active, "skapas avstängd")

    def test_404_when_utskick_is_off(self):
        UtskickSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        self.assertEqual(self.anna_client.get(self.url).status_code, 404)
        self.assertEqual(self.anna_client.post(self.url, {"title": "x"}).status_code, 404)

    def test_staff_in_view_as_acts_for_real(self):
        staff = self.client_for(self.staff)
        response = staff.post(self.url, {"title": "Anmälan", "is_active": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SignupForm.objects.get(account=self.account).is_active)
        self.assertFalse(SignupForm.objects.filter(account=self.other_account).exists())

    def test_without_dpa_the_reason_is_shown(self):
        DpaAcceptance.objects.filter(account=self.account).delete()
        response = self.anna_client.get(self.url)
        self.assertContains(response, "godkänn biträdesavtalet först")
