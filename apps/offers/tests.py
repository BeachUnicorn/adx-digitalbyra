"""
Offertsystemets vakter.

Det som INTE får gå sönder: token-länken är kundens enda behörighet (så
den måste vara oomgängligt lång och sidan noindex), en produkt som ändras
i katalogen får aldrig ändra en redan byggd offert, och Acceptera-knappen
är en affärshandling - den ska vara idempotent och bevisad med tidsstämpel.
"""

import json

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings

from .models import PricePeriod, Product, Quote, QuoteLine, QuoteStatus

EMAIL_SETTINGS = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "EMAIL_HOST_USER": "x",
    "EMAIL_HOST_PASSWORD": "y",
    "INQUIRY_NOTIFICATION_EMAIL": "staff@example.com",
}


def make_quote(**kwargs):
    defaults = {"customer_name": "Testkund AB", "customer_email": "kund@example.com"}
    defaults.update(kwargs)
    return Quote.objects.create(**defaults)


class StaffClientMixin:
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("byggare", password="hemligt123")

    def staff(self):
        client = Client()
        client.force_login(self.user)
        return client


class ManageAccessTests(StaffClientMixin, TestCase):
    def test_the_builder_requires_login(self):
        quote = make_quote()
        for url in (
            "/manage/offerter/",
            f"/manage/offerter/{quote.pk}/",
            "/manage/produkter/",
        ):
            with self.subTest(url=url):
                response = Client().get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/manage/login/", response["Location"])

    def test_the_builder_renders_for_logged_in_users(self):
        quote = make_quote()
        QuoteLine.objects.create(quote=quote, label="Hemsida", price=50000, order=1)
        response = self.staff().get(f"/manage/offerter/{quote.pk}/")
        self.assertContains(response, "Hemsida")
        self.assertContains(response, "Summering")


class ProductReuseTests(StaffClientMixin, TestCase):
    def test_a_product_is_copied_to_the_line_not_referenced_by_value(self):
        """Kärnan i återanvändningen: raden äger sina värden efter kopiering."""
        product = Product.objects.create(
            name="Förvaltning", default_price=1495, default_period=PricePeriod.MONTHLY
        )
        quote = make_quote()
        self.staff().post(
            f"/manage/offerter/{quote.pk}/rader/ny/", {"product_id": product.pk}
        )
        line = quote.lines.get()
        self.assertEqual((line.label, line.price, line.period), ("Förvaltning", 1495, "monthly"))

        # Katalogpriset ändras - raden ska stå kvar orörd.
        product.default_price = 9999
        product.save()
        line.refresh_from_db()
        self.assertEqual(line.price, 1495)

    def test_the_same_product_can_carry_different_prices_on_different_quotes(self):
        product = Product.objects.create(name="SEO", default_price=6500)
        client = self.staff()
        first, second = make_quote(), make_quote(customer_name="Andra kunden")
        for quote in (first, second):
            client.post(f"/manage/offerter/{quote.pk}/rader/ny/", {"product_id": product.pk})
        client.post(
            f"/manage/offerter/rad/{second.lines.get().pk}/uppdatera/",
            json.dumps({"price": "9 000"}),
            content_type="application/json",
        )
        self.assertEqual(first.lines.get().price, 6500)
        self.assertEqual(second.lines.get().price, 9000)

    def test_deleting_a_product_leaves_sent_quotes_intact(self):
        product = Product.objects.create(name="Copy", default_price=8500)
        quote = make_quote()
        self.staff().post(f"/manage/offerter/{quote.pk}/rader/ny/", {"product_id": product.pk})
        product.delete()
        line = quote.lines.get()
        self.assertEqual(line.label, "Copy")
        self.assertIsNone(line.product)


class LineEditingTests(StaffClientMixin, TestCase):
    def test_autosave_updates_fields_and_returns_totals(self):
        quote = make_quote()
        line = QuoteLine.objects.create(quote=quote, label="Rad", price=100, order=1)
        response = self.staff().post(
            f"/manage/offerter/rad/{line.pk}/uppdatera/",
            json.dumps({"label": "Hemsida", "price": "50000", "period": "one_time"}),
            content_type="application/json",
        )
        self.assertEqual(response.json()["totals"]["one_time"], 50000)
        line.refresh_from_db()
        self.assertEqual(line.label, "Hemsida")

    def test_price_cleaning_refuses_junk_and_negatives(self):
        quote = make_quote()
        line = QuoteLine.objects.create(quote=quote, label="Rad", price=100, order=1)
        client = self.staff()
        for junk in ("-500", "abc", ""):
            client.post(
                f"/manage/offerter/rad/{line.pk}/uppdatera/",
                json.dumps({"price": junk}),
                content_type="application/json",
            )
            line.refresh_from_db()
            self.assertGreaterEqual(line.price, 0)
        # "-500" saneras till siffrorna: 500. "abc" och "" blir 0.
        self.assertEqual(line.price, 0)

    def test_an_unknown_period_falls_back_to_one_time(self):
        quote = make_quote()
        line = QuoteLine.objects.create(
            quote=quote, label="Rad", period=PricePeriod.MONTHLY, order=1
        )
        self.staff().post(
            f"/manage/offerter/rad/{line.pk}/uppdatera/",
            json.dumps({"period": "weekly"}),
            content_type="application/json",
        )
        line.refresh_from_db()
        self.assertEqual(line.period, "one_time")


class ReorderTests(StaffClientMixin, TestCase):
    def test_drag_and_drop_order_is_persisted(self):
        quote = make_quote()
        a, b, c = (
            QuoteLine.objects.create(quote=quote, label=label, order=i)
            for i, label in enumerate(["A", "B", "C"], start=1)
        )
        response = self.staff().post(
            f"/manage/offerter/{quote.pk}/rader/ordna/",
            json.dumps({"order": [c.pk, a.pk, b.pk]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(quote.lines.values_list("label", flat=True)), ["C", "A", "B"])

    def test_reorder_refuses_foreign_or_incomplete_id_sets(self):
        quote, other = make_quote(), make_quote(customer_name="Annan")
        mine = QuoteLine.objects.create(quote=quote, label="Min", order=1)
        theirs = QuoteLine.objects.create(quote=other, label="Deras", order=1)
        for bad in ([theirs.pk], [], [mine.pk, theirs.pk]):
            response = self.staff().post(
                f"/manage/offerter/{quote.pk}/rader/ordna/",
                json.dumps({"order": bad}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400)


class PublicPageTests(TestCase):
    def test_the_token_is_long_and_the_page_is_noindex(self):
        quote = make_quote()
        self.assertGreaterEqual(len(quote.token), 30)
        response = Client().get(quote.get_public_url())
        self.assertContains(response, "noindex")
        self.assertContains(response, "Testkund AB")

    def test_a_wrong_token_is_a_404(self):
        make_quote()
        self.assertEqual(Client().get("/offert/felaktig-token/").status_code, 404)

    def test_an_anonymous_visit_marks_the_quote_opened_once(self):
        quote = make_quote(status=QuoteStatus.SENT)
        Client().get(quote.get_public_url())
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.OPENED)
        first_opened = quote.opened_at
        Client().get(quote.get_public_url())
        quote.refresh_from_db()
        self.assertEqual(quote.opened_at, first_opened)

    def test_a_logged_in_preview_does_not_count_as_opened(self):
        user = get_user_model().objects.create_user("giovanni2", password="x12345678")
        quote = make_quote(status=QuoteStatus.SENT)
        client = Client()
        client.force_login(user)
        client.get(quote.get_public_url())
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.SENT)

    def test_periods_render_with_their_suffixes(self):
        quote = make_quote(status=QuoteStatus.SENT)
        QuoteLine.objects.create(quote=quote, label="Bygge", price=50000, order=1)
        QuoteLine.objects.create(
            quote=quote, label="Drift", price=1495, period=PricePeriod.MONTHLY, order=2
        )
        QuoteLine.objects.create(
            quote=quote, label="Domän", price=500, period=PricePeriod.YEARLY, order=3
        )
        html = Client().get(quote.get_public_url()).content.decode()
        self.assertIn("50 000 kr", html)
        self.assertIn("1 495 kr/mån", html)
        self.assertIn("500 kr/år", html)
        self.assertIn("Att betala vid leverans", html)

    def test_robots_txt_blocks_the_offer_area(self):
        self.assertIn("Disallow: /offert/", Client().get("/robots.txt").content.decode())


@override_settings(**EMAIL_SETTINGS)
class AcceptFlowTests(TestCase):
    def test_accepting_sets_status_evidence_and_emails_staff(self):
        quote = make_quote(status=QuoteStatus.OPENED)
        response = Client().post(f"/offert/{quote.token}/acceptera/")
        self.assertEqual(response.status_code, 302)
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.ACCEPTED)
        self.assertIsNotNone(quote.accepted_at)
        self.assertTrue(quote.accepted_ip)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ACCEPTERAD", mail.outbox[0].subject)

    def test_accepting_twice_does_not_double_anything(self):
        quote = make_quote(status=QuoteStatus.SENT)
        client = Client()
        client.post(f"/offert/{quote.token}/acceptera/")
        first = Quote.objects.get(pk=quote.pk).accepted_at
        client.post(f"/offert/{quote.token}/acceptera/")
        quote.refresh_from_db()
        self.assertEqual(quote.accepted_at, first)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_draft_cannot_be_accepted(self):
        quote = make_quote(status=QuoteStatus.DRAFT)
        Client().post(f"/offert/{quote.token}/acceptera/")
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.DRAFT)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_accept_button_only_shows_when_answerable(self):
        answerable = make_quote(status=QuoteStatus.SENT)
        accepted = make_quote(customer_name="Klar kund", status=QuoteStatus.ACCEPTED)
        self.assertContains(Client().get(answerable.get_public_url()), "Acceptera offerten")
        self.assertNotContains(Client().get(accepted.get_public_url()), "Acceptera offerten")

    def test_a_question_reaches_staff_with_reply_to_customer(self):
        quote = make_quote(status=QuoteStatus.SENT)
        response = Client().post(
            f"/offert/{quote.token}/fraga/", {"message": "Ingår hosting?"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Ingår hosting?", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].reply_to, ["kund@example.com"])


@override_settings(**EMAIL_SETTINGS)
class SendFlowTests(StaffClientMixin, TestCase):
    def test_sending_emails_the_customer_and_marks_sent(self):
        quote = make_quote()
        QuoteLine.objects.create(quote=quote, label="Hemsida", price=50000, order=1)
        self.staff().post(f"/manage/offerter/{quote.pk}/skicka/")
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.SENT)
        self.assertIsNotNone(quote.sent_at)
        self.assertEqual(mail.outbox[0].to, ["kund@example.com"])
        self.assertIn(quote.token, mail.outbox[0].body)

    def test_sending_without_email_or_lines_is_refused(self):
        no_email = make_quote(customer_email="")
        QuoteLine.objects.create(quote=no_email, label="Rad", order=1)
        no_lines = make_quote(customer_name="Tom offert")
        client = self.staff()
        for quote in (no_email, no_lines):
            client.post(f"/manage/offerter/{quote.pk}/skicka/")
            quote.refresh_from_db()
            self.assertEqual(quote.status, QuoteStatus.DRAFT)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_accepted_quote_cannot_be_deleted(self):
        quote = make_quote(status=QuoteStatus.ACCEPTED)
        self.staff().post(f"/manage/offerter/{quote.pk}/ta-bort/")
        self.assertTrue(Quote.objects.filter(pk=quote.pk).exists())

    def test_resending_never_regresses_an_accepted_status(self):
        """
        Racet granskningen hittade: kunden accepterar medan omsändningens
        SMTP-anrop pågår. De villkorade UPDATE:arna får aldrig skriva
        tillbaka den stallästa statusen.
        """
        from unittest.mock import patch

        quote = make_quote(status=QuoteStatus.OPENED)
        QuoteLine.objects.create(quote=quote, label="Rad", price=100, order=1)

        def accept_mid_send(q):
            Quote.objects.filter(pk=q.pk).update(status=QuoteStatus.ACCEPTED)
            return True

        with patch("apps.offers.manage_views.send_quote_to_customer", side_effect=accept_mid_send):
            self.staff().post(f"/manage/offerter/{quote.pk}/skicka/")
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.ACCEPTED)


class LockedQuoteTests(StaffClientMixin, TestCase):
    """En accepterad offert är en affärshandling - innehållet är fryst."""

    def setUp(self):
        self.quote = make_quote(status=QuoteStatus.ACCEPTED)
        self.line = QuoteLine.objects.create(
            quote=self.quote, label="Hemsida", price=50000, order=1
        )

    def test_lines_cannot_be_edited_added_removed_or_reordered(self):
        client = self.staff()
        response = client.post(
            f"/manage/offerter/rad/{self.line.pk}/uppdatera/",
            json.dumps({"price": "1"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        client.post(f"/manage/offerter/{self.quote.pk}/rader/ny/", {})
        client.post(f"/manage/offerter/rad/{self.line.pk}/ta-bort/")
        response = client.post(
            f"/manage/offerter/{self.quote.pk}/rader/ordna/",
            json.dumps({"order": [self.line.pk]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.line.refresh_from_db()
        self.assertEqual((self.line.price, self.quote.lines.count()), (50000, 1))

    def test_customer_fields_cannot_be_edited(self):
        response = self.staff().post(
            f"/manage/offerter/{self.quote.pk}/uppdatera/",
            json.dumps({"customer_name": "Nytt namn"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.customer_name, "Testkund AB")


class ReviewRegressionTests(StaffClientMixin, TestCase):
    """Övriga fynd från granskningen, låsta som tester."""

    def test_offer_pages_are_never_written_to_analytics(self):
        """Token är behörigheten - den får inte loggas som besöksdata."""
        from apps.analytics.models import PageView, Session, Visitor

        quote = make_quote(status=QuoteStatus.SENT)
        Client(HTTP_USER_AGENT="Mozilla/5.0 (Macintosh) Chrome/128").get(
            quote.get_public_url()
        )
        for model in (PageView, Session, Visitor):
            self.assertEqual(model.objects.count(), 0)

    def test_autosave_does_not_touch_the_order_field(self):
        quote = make_quote()
        line = QuoteLine.objects.create(quote=quote, label="Rad", price=1, order=7)
        QuoteLine.objects.filter(pk=line.pk).update(order=3)  # samtidig omordning
        self.staff().post(
            f"/manage/offerter/rad/{line.pk}/uppdatera/",
            json.dumps({"label": "Ny etikett"}),
            content_type="application/json",
        )
        line.refresh_from_db()
        self.assertEqual((line.label, line.order), ("Ny etikett", 3))

    def test_long_field_values_truncate_instead_of_500(self):
        quote = make_quote()
        response = self.staff().post(
            f"/manage/offerter/{quote.pk}/uppdatera/",
            json.dumps({"customer_name": "x" * 600, "intro": "y" * 6000}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        quote.refresh_from_db()
        self.assertEqual(len(quote.customer_name), 200)
        self.assertEqual(len(quote.intro), 5000)

    @override_settings(**EMAIL_SETTINGS)
    def test_two_simultaneous_accepts_send_one_email(self):
        """Villkorad UPDATE: bara requesten som vann övergången mejlar."""
        from apps.offers import public_views
        from apps.offers.models import Quote as QuoteModel

        quote = make_quote(status=QuoteStatus.OPENED)
        # Simulera att en annan request hann före: statusen är redan
        # accepterad när den här requestens UPDATE körs.
        QuoteModel.objects.filter(pk=quote.pk).update(status=QuoteStatus.ACCEPTED)
        Client().post(f"/offert/{quote.token}/acceptera/")
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNotNone(public_views)  # håll importen ärlig

    def test_a_failed_question_email_is_reported_not_swallowed(self):
        # Ingen EMAIL-konfiguration alls -> sändningen returnerar False.
        quote = make_quote(status=QuoteStatus.SENT)
        with override_settings(EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD=""):
            response = Client().post(
                f"/offert/{quote.token}/fraga/", {"message": "Hallå?"}, follow=True
            )
        self.assertContains(response, "kunde inte skickas")
