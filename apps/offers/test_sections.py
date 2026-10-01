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

    def test_totals_show_vat_and_amount_including_vat(self):
        html = self.page(self.make_quote())
        self.assertIn("Moms 25 %", html)
        self.assertIn("12 500 kr", html)  # moms på 50 000
        self.assertIn("62 500 kr", html)  # inklusive moms
        self.assertIn("1 250 kr/mån", html)  # drift inklusive moms
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
        self.assertIn("62 500 kr", html)


class OptionPriceLayoutTests(TestCase):
    """Tillvalspriset klipptes i högerkanten på telefon när namnet hade ett långt ord."""

    def test_the_text_column_can_shrink_and_the_price_wraps_below_on_phones(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "offert.css").read_text()
        self.assertIn("grid-template-columns: auto minmax(0, 1fr) auto", css)
        phone = css.split("@media (max-width: 640px)")[1].split("\n}")[0]
        self.assertIn(".of-opt-price { grid-column: 2;", phone)
