"""
Offertsidans nya sektioner (2026-10-01): Det här ingår, Pris/Löpande,
moms, villkor, kontaktperson - och tillvalspriset som klipptes på telefon.
"""

from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.offers.models import OfferText, PricePeriod, Quote, QuoteLine, QuoteStatus, TextKind


@override_settings(CUSTOMER_REPLY_TO_EMAIL="giovanni@adx.se")
class SectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(
            "giovanni.palermo", password="x12345678", is_staff=True
        )
        OfferText.objects.all().delete()
        OfferText.objects.create(
            kind=TextKind.TERMS, name="Standard", text="Fakturering: 30 dagar.", is_default=True
        )

    def make_quote(self, **extra):
        quote = Quote.objects.create(
            customer_name="Acme AB", project_title="Ny hemsida", created_by=self.staff, **extra
        )
        QuoteLine.objects.create(
            quote=quote, label="Hemsida", price=50000, period=PricePeriod.ONE_TIME, order=1
        )
        QuoteLine.objects.create(
            quote=quote, label="Drift", price=1000, period=PricePeriod.MONTHLY, order=2
        )
        Quote.objects.filter(pk=quote.pk).update(status=QuoteStatus.SENT)
        quote.refresh_from_db()
        return quote

    def page(self, quote):
        return self.client.get(quote.get_public_url()).content.decode()

    def test_includes_section_shows_title_and_explanation_and_hides_when_empty(self):
        quote = self.make_quote(includes="Design: Ett förslag ni godkänner.\nMobil och dator")
        html = self.page(quote)
        self.assertIn("Det här ingår", html)
        self.assertIn("<b>Design</b><span>Ett förslag ni godkänner.</span>", html)
        self.assertIn("<b>Mobil och dator</b>", html)
        self.assertNotIn("Det här ingår", self.page(self.make_quote()))

    def test_one_time_and_recurring_lines_are_split(self):
        html = self.page(self.make_quote())
        pris = html.index(">Pris<")
        lopande = html.index(">Löpande<")
        self.assertLess(pris, html.index("Hemsida"))
        self.assertLess(html.index("Hemsida"), lopande)
        self.assertLess(lopande, html.index("Drift"))
        self.assertNotIn(">Ingår<", html)

    def test_totals_specify_vat_but_never_a_total_including_vat(self):
        """Giovanni 2026-10-01: momsen specificeras, totalen räknar kunden själv."""
        html = self.page(self.make_quote())
        self.assertIn("Moms 25 %", html)
        self.assertIn("12 500 kr", html)  # moms på 50 000
        self.assertIn("250 kr/mån", html)  # moms på driften
        self.assertNotIn("62 500", html)
        self.assertNotIn("1 250", html)
        self.assertNotIn("inklusive moms", html)
        self.assertIn('data-vat-rate="25"', html)
        # Inga årsrader här, och dolda rader ska förbli dolda trots display:flex.
        self.assertIn('id="row-yearly" hidden', html)
        css = (Path(settings.BASE_DIR) / "static" / "css" / "offert.css").read_text()
        self.assertIn(".of-trow[hidden] { display: none; }", css)

    def test_new_quotes_get_the_default_terms_copied_in(self):
        quote = self.make_quote()
        self.assertEqual(quote.terms, "Fakturering: 30 dagar.")
        # Ändras mallen ändras inte offerten - den är en affärshandling.
        OfferText.objects.filter(kind=TextKind.TERMS).update(text="Något annat.")
        quote.refresh_from_db()
        self.assertEqual(quote.terms, "Fakturering: 30 dagar.")

    def test_terms_list_starts_with_vat_and_validity(self):
        from datetime import date

        quote = self.make_quote(valid_until=date(2026, 10, 31))
        lines = quote.terms_lines()
        self.assertEqual(lines[0], "Alla priser i svenska kronor, exklusive moms.")
        self.assertIn("31 oktober 2026", lines[1])
        self.assertEqual(lines[2], "Fakturering: 30 dagar.")
        self.assertIn("Villkor", self.page(quote))

    def test_contact_card_uses_the_account_and_the_reply_address(self):
        quote = self.make_quote()
        contact = quote.contact()
        self.assertEqual(contact["name"], "Giovanni Palermo")  # ur användarnamnet
        self.assertEqual(contact["email"], "giovanni@adx.se")
        self.assertEqual(contact["initials"], "GP")
        html = self.page(quote)
        self.assertIn("Er kontakt på ADX", html)
        self.assertIn("ADX, Giovanni Palermo", html)

    def test_no_contact_without_an_author(self):
        quote = Quote.objects.create(customer_name="Acme AB")
        self.assertIsNone(quote.contact())

    def test_editor_saves_the_texts_and_they_lock_on_accept(self):
        quote = self.make_quote()
        self.client.force_login(self.staff)
        url = f"/manage/offerter/{quote.pk}/uppdatera/"
        r = self.client.post(
            url, {"includes": "A: B", "terms": "C"}, content_type="application/json"
        )
        self.assertEqual(r.status_code, 200)
        quote.refresh_from_db()
        self.assertEqual((quote.includes, quote.terms), ("A: B", "C"))
        Quote.objects.filter(pk=quote.pk).update(status=QuoteStatus.ACCEPTED)
        r = self.client.post(url, {"terms": "Ändrat"}, content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_templates_have_one_default_and_duplicate_copies_texts(self):
        self.client.force_login(self.staff)
        self.client.post(
            "/manage/offerttexter/ny/",
            {"name": "Nya villkor", "kind": "terms", "text": "X", "is_default": "on"},
        )
        self.assertEqual(OfferText.objects.filter(kind=TextKind.TERMS, is_default=True).count(), 1)
        self.assertEqual(OfferText.objects.get(is_default=True).name, "Nya villkor")
        quote = self.make_quote(includes="Punkt")
        copy = quote.duplicate(customer_name="Annan AB")
        self.assertEqual((copy.includes, copy.terms), (quote.includes, quote.terms))

    def test_accept_page_shows_vat_too(self):
        quote = self.make_quote()
        html = self.client.get(f"/offert/{quote.token}/acceptera/").content.decode()
        self.assertIn("Moms 25 %", html)
        self.assertIn("12 500 kr", html)
        self.assertNotIn("62 500", html)

    def test_the_customer_never_sees_the_offer_number(self):
        """Numret avslöjar hur många offerter byrån skapat."""
        quote = self.make_quote()
        for url in (quote.get_public_url(), f"/offert/{quote.token}/acceptera/"):
            html = self.client.get(url).content.decode()
            kicker = html.split('class="of-kicker"')[1].split("</p>")[0]
            self.assertNotIn(str(quote.pk), kicker, url)


class OptionPriceLayoutTests(TestCase):
    """Tillvalspriset klipptes i högerkanten på telefon när namnet hade ett långt ord."""

    def test_the_text_column_can_shrink_and_the_price_wraps_below_on_phones(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "offert.css").read_text()
        self.assertIn("grid-template-columns: auto minmax(0, 1fr) auto", css)
        phone = css.split("@media (max-width: 640px)")[1].split("\n}")[0]
        self.assertIn(".of-opt-price { grid-column: 2;", phone)


class ShareLinkTests(TestCase):
    """Länken till kunden utan mejl: kunder utan e-post ska också kunna acceptera."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            "byra", password="x12345678", is_staff=True
        )

    def setUp(self):
        self.quote = Quote.objects.create(customer_name="Verkstad AB", created_by=self.staff)
        QuoteLine.objects.create(quote=self.quote, label="Hemsida", price=24995, order=1)
        self.client.force_login(self.staff)

    def share(self):
        return self.client.post(f"/manage/offerter/{self.quote.pk}/dela/")

    def test_a_draft_cannot_be_accepted_until_the_link_is_activated(self):
        from django.core import mail
        from django.test import Client

        customer = Client()
        self.assertNotIn(
            "Acceptera offerten", customer.get(self.quote.get_public_url()).content.decode()
        )
        self.share()
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.SENT)
        self.assertIsNotNone(self.quote.sent_at)
        self.assertEqual(len(mail.outbox), 0, "inget mejl")
        self.assertIn(
            "Acceptera offerten", customer.get(self.quote.get_public_url()).content.decode()
        )

    def test_the_editor_shows_the_full_link_to_copy_once_active(self):
        page = self.client.get(f"/manage/offerter/{self.quote.pk}/").content.decode()
        self.assertIn("Ta fram länk utan mejl", page)
        self.assertNotIn('id="o-share-url"', page)
        self.share()
        page = self.client.get(f"/manage/offerter/{self.quote.pk}/").content.decode()
        self.assertIn('id="o-share-url"', page)
        self.assertIn(f"{self.quote.get_public_url()}", page)
        self.assertIn('data-copy-target="o-share-url"', page)

    def test_accepted_and_empty_offers_are_refused(self):
        empty = Quote.objects.create(customer_name="Tom AB")
        self.client.post(f"/manage/offerter/{empty.pk}/dela/")
        empty.refresh_from_db()
        self.assertEqual(empty.status, QuoteStatus.DRAFT)
        Quote.objects.filter(pk=self.quote.pk).update(status=QuoteStatus.ACCEPTED)
        self.share()
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.ACCEPTED)


class DeclineTests(TestCase):
    """Kunden kan tacka nej: två steg, skäl valfritt, notis till byrån, går att öppna igen."""

    EMAIL = {
        "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
        "EMAIL_HOST_USER": "x",
        "EMAIL_HOST_PASSWORD": "x",
        "INQUIRY_NOTIFICATION_EMAIL": "byran@example.com",
    }

    def setUp(self):
        self.quote = Quote.objects.create(
            customer_name="Verkstad AB", customer_email="kund@example.com"
        )
        QuoteLine.objects.create(quote=self.quote, label="Hemsida", price=24995, order=1)
        Quote.objects.filter(pk=self.quote.pk).update(status=QuoteStatus.SENT)

    def test_the_offer_page_has_a_red_decline_button_next_to_accept(self):
        html = self.client.get(self.quote.get_public_url()).content.decode()
        self.assertIn('class="of-decline"', html)
        self.assertIn(f"/offert/{self.quote.token}/tacka-nej/", html)
        css = (Path(settings.BASE_DIR) / "static" / "css" / "offert.css").read_text()
        rule = css.split(".of-decline {")[1].split("}")[0]
        self.assertIn("background: var(--s-danger)", rule)

    def test_the_first_click_only_shows_the_confirmation_page(self):
        r = self.client.get(f"/offert/{self.quote.token}/tacka-nej/")
        self.assertContains(r, "Varför tackar ni nej?")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.SENT)

    def test_declining_records_the_reason_and_notifies_the_agency_once(self):
        from django.core import mail

        with self.settings(**self.EMAIL):
            url = f"/offert/{self.quote.token}/tacka-nej/"
            self.client.post(url, {"reason": "price", "message": "För dyrt för oss just nu."})
            self.client.post(url, {"reason": "timing"})  # dubbelklick
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.DECLINED)
        self.assertEqual(self.quote.decline_reason, "price")
        self.assertIsNotNone(self.quote.declined_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["byran@example.com"])
        self.assertIn("Priset passar inte", mail.outbox[0].body)
        self.assertIn("För dyrt för oss just nu.", mail.outbox[0].body)
        page = self.client.get(self.quote.get_public_url()).content.decode()
        self.assertIn("Ni har tackat nej", page)
        self.assertNotIn("Acceptera offerten", page)

    def test_no_reason_is_fine_and_an_unknown_reason_is_dropped(self):
        self.client.post(f"/offert/{self.quote.token}/tacka-nej/", {"reason": "<script>"})
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.DECLINED)
        self.assertEqual(self.quote.decline_reason, "")

    def test_an_accepted_offer_cannot_be_declined(self):
        Quote.objects.filter(pk=self.quote.pk).update(status=QuoteStatus.ACCEPTED)
        self.client.post(f"/offert/{self.quote.token}/tacka-nej/", {"reason": "price"})
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.ACCEPTED)

    def test_staff_sees_the_reason_and_can_reopen(self):
        staff = get_user_model().objects.create_user("byra", password="x12345678", is_staff=True)
        self.client.post(f"/offert/{self.quote.token}/tacka-nej/", {"reason": "scope"})
        self.client.force_login(staff)
        page = self.client.get(f"/manage/offerter/{self.quote.pk}/").content.decode()
        self.assertIn("Kunden tackade nej", page)
        self.assertIn("Förslaget passar inte våra behov", page)
        self.client.post(f"/manage/offerter/{self.quote.pk}/status/", {"status": "sent"})
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.SENT)
        self.assertIsNone(self.quote.declined_at)
        self.assertEqual(self.quote.decline_reason, "scope", "skälet sparas som historik")
