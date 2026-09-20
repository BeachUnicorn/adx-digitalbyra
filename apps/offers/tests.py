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


#: Beställarens uppgifter - obligatoriska sedan acceptsidan (2026-09-20).
ACCEPT = {
    "first_name": "Nina",
    "last_name": "Nordan",
    "email": "nina@nordan.se",
    "phone": "070-123 45 67",
    "company": "Nordan Bygg AB",
    "org_number": "556712-3456",
    "billing_address": "Byggvägen 1, 123 45 Stockholm",
    "confirm": "on",
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
        self.staff().post(f"/manage/offerter/{quote.pk}/rader/ny/", {"product_id": product.pk})
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
class OptionalLineTests(TestCase):
    """Tillvalen: kunden väljer med togglar, valet blir en del av accepten."""

    def setUp(self):
        self.quote = make_quote(status=QuoteStatus.SENT)
        self.base = QuoteLine.objects.create(
            quote=self.quote, label="Hemsida", price=50000, order=1
        )
        self.seo = QuoteLine.objects.create(
            quote=self.quote,
            label="SEO",
            price=6500,
            order=2,
            is_optional=True,
            is_selected=True,
        )
        self.booking = QuoteLine.objects.create(
            quote=self.quote,
            label="Bokning",
            price=12000,
            order=3,
            is_optional=True,
            is_selected=False,
        )

    def test_totals_count_only_selected_options(self):
        self.assertEqual(self.quote.totals()["one_time"], 56500)

    def test_the_public_page_renders_options_as_toggles(self):
        import re

        html = Client().get(self.quote.get_public_url()).content.decode()
        self.assertIn('name="tillval"', html)
        self.assertIn("Tillval", html)
        # Förvalet styr checked-attributet: SEO förvald, Bokning inte.
        toggles = dict(re.findall(r'value="(\d+)"[^>]*?(checked)?>\s*<span class="switch"', html))
        self.assertEqual(toggles.get(str(self.seo.pk)), "checked")
        self.assertEqual(toggles.get(str(self.booking.pk)), "")

    def test_accepting_with_choices_persists_the_customers_selection(self):
        Client().post(
            f"/offert/{self.quote.token}/acceptera/", {**ACCEPT, "tillval": [str(self.booking.pk)]}
        )
        self.seo.refresh_from_db()
        self.booking.refresh_from_db()
        self.quote.refresh_from_db()
        self.assertFalse(self.seo.is_selected)
        self.assertTrue(self.booking.is_selected)
        self.assertEqual(self.quote.totals()["one_time"], 62000)
        self.assertIn("Bokning", mail.outbox[0].body)
        self.assertIn("VALDE BORT: SEO", mail.outbox[0].body)

    def test_foreign_line_ids_cannot_be_smuggled_into_the_selection(self):
        other = make_quote(customer_name="Annan", status=QuoteStatus.SENT)
        foreign = QuoteLine.objects.create(
            quote=other,
            label="Främmande",
            price=1,
            order=1,
            is_optional=True,
            is_selected=False,
        )
        Client().post(
            f"/offert/{self.quote.token}/acceptera/", {**ACCEPT, "tillval": [str(foreign.pk)]}
        )
        foreign.refresh_from_db()
        self.base.refresh_from_db()
        self.assertFalse(foreign.is_selected, "annan offerts rad får inte påverkas")
        self.assertTrue(self.base.is_selected, "fasta rader rörs aldrig av valet")

    def test_after_accept_the_page_shows_chosen_options_without_toggles(self):
        Client().post(
            f"/offert/{self.quote.token}/acceptera/", {**ACCEPT, "tillval": [str(self.seo.pk)]}
        )
        html = Client().get(self.quote.get_public_url()).content.decode()
        self.assertNotIn('type="checkbox" name="tillval"', html)
        self.assertIn("SEO", html)
        self.assertNotIn("Bokning", html)


@override_settings(**EMAIL_SETTINGS)
class AcceptFlowTests(TestCase):
    def test_accepting_sets_status_evidence_and_emails_staff(self):
        quote = make_quote(status=QuoteStatus.OPENED)
        response = Client().post(f"/offert/{quote.token}/acceptera/", ACCEPT)
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
        client.post(f"/offert/{quote.token}/acceptera/", ACCEPT)
        first = Quote.objects.get(pk=quote.pk).accepted_at
        client.post(f"/offert/{quote.token}/acceptera/", ACCEPT)
        quote.refresh_from_db()
        self.assertEqual(quote.accepted_at, first)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_draft_cannot_be_accepted(self):
        quote = make_quote(status=QuoteStatus.DRAFT)
        Client().post(f"/offert/{quote.token}/acceptera/", ACCEPT)
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
        response = Client().post(f"/offert/{quote.token}/fraga/", {"message": "Ingår hosting?"})
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
        Client(HTTP_USER_AGENT="Mozilla/5.0 (Macintosh) Chrome/128").get(quote.get_public_url())
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
        Client().post(f"/offert/{quote.token}/acceptera/", ACCEPT)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNotNone(public_views)  # håll importen ärlig

    def test_optional_flags_are_saved_via_autosave(self):
        quote = make_quote()
        line = QuoteLine.objects.create(quote=quote, label="SEO", price=6500, order=1)
        self.staff().post(
            f"/manage/offerter/rad/{line.pk}/uppdatera/",
            json.dumps({"is_optional": True, "is_selected": False}),
            content_type="application/json",
        )
        line.refresh_from_db()
        self.assertTrue(line.is_optional)
        self.assertFalse(line.is_selected)

    def test_a_failed_question_email_is_reported_not_swallowed(self):
        # Ingen EMAIL-konfiguration alls -> sändningen returnerar False.
        quote = make_quote(status=QuoteStatus.SENT)
        with override_settings(EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD=""):
            response = Client().post(
                f"/offert/{quote.token}/fraga/", {"message": "Hallå?"}, follow=True
            )
        self.assertContains(response, "kunde inte skickas")


@override_settings(**EMAIL_SETTINGS)
class ProjectLinkTests(StaffClientMixin, TestCase):
    """
    Offert -> projekt -> ärenden. Kopplingen är manuell (aldrig vid accept),
    idempotent (rader minns sitt ärende) och mejlar aldrig någon.
    """

    def _update(self, quote, payload):
        return self.staff().post(
            f"/manage/offerter/{quote.pk}/uppdatera/",
            json.dumps(payload),
            content_type="application/json",
        )

    def test_autosave_links_and_unlinks_a_project(self):
        from apps.projects.models import Project

        project = Project.objects.create(name="Nordan Bygg", key="NORD")
        quote = make_quote()
        self.assertEqual(self._update(quote, {"project": str(project.pk)}).status_code, 200)
        quote.refresh_from_db()
        self.assertEqual(quote.project, project)
        self.assertIsNone(quote.customer, "utan kund på projektet finns ingen kund")

        self.assertEqual(self._update(quote, {"project": ""}).status_code, 200)
        quote.refresh_from_db()
        self.assertIsNone(quote.project)

    def test_an_unknown_project_id_is_ignored(self):
        quote = make_quote()
        response = self._update(quote, {"project": "999999"})
        self.assertEqual(response.status_code, 400)
        quote.refresh_from_db()
        self.assertIsNone(quote.project)

    def test_the_link_is_outside_the_lock_but_the_content_is_not(self):
        from apps.projects.models import Project

        project = Project.objects.create(name="Nordan Bygg", key="NORD")
        quote = make_quote(status=QuoteStatus.ACCEPTED)
        self.assertEqual(self._update(quote, {"project": str(project.pk)}).status_code, 200)
        response = self._update(quote, {"customer_name": "Nytt namn"})
        self.assertEqual(response.status_code, 400)
        quote.refresh_from_db()
        self.assertEqual((quote.project, quote.customer_name), (project, "Testkund AB"))

    def test_the_editor_offers_active_projects_only(self):
        from apps.projects.models import Project, ProjectStatus

        Project.objects.create(name="Aktivt", key="AKT")
        Project.objects.create(name="Gammalt", key="GAM", status=ProjectStatus.ARCHIVED)
        html = self.staff().get(f"/manage/offerter/{make_quote().pk}/").content.decode()
        self.assertIn('data-field="project"', html)
        self.assertIn("AKT - Aktivt", html)
        self.assertNotIn("GAM - Gammalt", html)

    def test_creating_issues_builds_customer_project_and_issues_once(self):
        from apps.projects.models import Customer, Issue, Project

        # Befintlig kund med annat skiftläge: ska återanvändas, inte dubbleras.
        existing = Customer.objects.create(name="testkund ab")
        quote = make_quote(project_title="Ny hemsida")
        fixed = QuoteLine.objects.create(
            quote=quote, label="Hemsida", description="Fem sidor", price=50000, order=1
        )
        chosen = QuoteLine.objects.create(
            quote=quote, label="SEO", price=6500, order=2, is_optional=True, is_selected=True
        )
        skipped = QuoteLine.objects.create(
            quote=quote,
            label="Bokning",
            price=12000,
            order=3,
            is_optional=True,
            is_selected=False,
        )

        response = self.staff().post(f"/manage/offerter/{quote.pk}/arenden/", follow=True)
        self.assertContains(response, "2 ärenden skapade i")

        quote.refresh_from_db()
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(quote.customer, existing)
        project = Project.objects.get()
        self.assertEqual(quote.project, project)
        self.assertEqual(project.name, "Ny hemsida")
        self.assertEqual((project.customer, project.created_by), (existing, self.user))

        for line in (fixed, chosen, skipped):
            line.refresh_from_db()
        self.assertIsNone(skipped.issue, "ovalda tillval blir inte ärenden")
        self.assertEqual(
            (fixed.issue.title, fixed.issue.description, fixed.issue.project),
            ("Hemsida", "Fem sidor", project),
        )
        self.assertEqual(fixed.issue.column, project.columns.order_by("position").first())
        self.assertTrue(fixed.issue.is_billable)
        self.assertEqual(fixed.issue.reporter, self.user)
        self.assertIn(f"offert #{quote.pk}", fixed.issue.activity.get().text)
        self.assertEqual(Issue.objects.count(), 2)

        # Andra körningen: inga dubbletter, tydligt besked.
        response = self.staff().post(f"/manage/offerter/{quote.pk}/arenden/", follow=True)
        self.assertContains(response, "Alla rader har redan ärenden")
        self.assertEqual(Issue.objects.count(), 2)
        self.assertEqual(Project.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 0, "att skapa ärenden mejlar aldrig")

        # Redigeraren visar ärendenyckeln vid raden.
        html = self.staff().get(f"/manage/offerter/{quote.pk}/").content.decode()
        self.assertIn(fixed.issue.key, html)
        self.assertIn(f"/manage/arenden/{fixed.issue.pk}/", html)

    def test_creating_issues_uses_the_linked_project_and_makes_no_customer(self):
        from apps.projects.models import Customer, Project

        project = Project.objects.create(name="Internt", key="INT")
        quote = make_quote(project=project)
        QuoteLine.objects.create(quote=quote, label="Rad", order=1)
        self.staff().post(f"/manage/offerter/{quote.pk}/arenden/")
        self.assertEqual(Customer.objects.count(), 0)
        self.assertEqual(Project.objects.count(), 1)
        self.assertEqual(project.issues.get().title, "Rad")

    def test_a_new_project_falls_back_to_the_customer_name(self):
        from apps.projects.models import Project

        quote = make_quote(project_title="")
        QuoteLine.objects.create(quote=quote, label="Rad", order=1)
        self.staff().post(f"/manage/offerter/{quote.pk}/arenden/")
        self.assertEqual(Project.objects.get().name, "Offert Testkund AB")

    def test_accepting_never_creates_issues(self):
        from apps.projects.models import Issue, Project

        quote = make_quote(status=QuoteStatus.SENT)
        QuoteLine.objects.create(quote=quote, label="Hemsida", price=50000, order=1)
        Client().post(f"/offert/{quote.token}/acceptera/", ACCEPT)
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.ACCEPTED)
        self.assertEqual((Issue.objects.count(), Project.objects.count()), (0, 0))

    def test_the_list_shows_the_project_key(self):
        from apps.projects.models import Project

        project = Project.objects.create(name="Nordan Bygg", key="NORD")
        make_quote(project=project, project_title="Fritext som inte ska visas")
        make_quote(customer_name="Annan", project_title="Bara fritext")
        html = self.staff().get("/manage/offerter/").content.decode()
        self.assertIn("NORD", html)
        self.assertNotIn("Fritext som inte ska visas", html)
        self.assertIn("Bara fritext", html)


class ProductSeedTests(TestCase):
    """Katalogseeden är additiv och hittar aldrig på priser."""

    def test_seed_creates_the_catalog_and_never_overwrites(self):
        from django.core.management import call_command

        Product.objects.create(name="Fotografering", default_price=7500)
        call_command("seed_produkter", verbosity=0)
        self.assertGreater(Product.objects.count(), 30)
        self.assertEqual(Product.objects.get(name="Fotografering").default_price, 7500)
        before = Product.objects.count()
        call_command("seed_produkter", verbosity=0)
        self.assertEqual(Product.objects.count(), before)

    def test_only_established_prices_are_seeded(self):
        from django.core.management import call_command

        call_command("seed_produkter", verbosity=0)
        priced = set(Product.objects.exclude(default_price=0).values_list("name", flat=True))
        self.assertEqual(priced, {"Skräddarsydd hemsida", "Hemsida via Atlas Holly"})


@override_settings(**EMAIL_SETTINGS)
class AcceptPageTests(TestCase):
    """Accepten är två steg: välj tillval -> beställarens uppgifter -> kvitto."""

    def setUp(self):
        self.quote = Quote.objects.create(
            customer_name="Nordan Bygg AB",
            customer_email="info@nordan.se",
            project_title="Ny hemsida",
            status=QuoteStatus.SENT,
        )
        QuoteLine.objects.create(quote=self.quote, label="Hemsida", price=50000, order=1)
        self.foto = QuoteLine.objects.create(
            quote=self.quote, label="Foto", price=9500, is_optional=True, is_selected=False, order=2
        )
        self.url = f"/offert/{self.quote.token}/acceptera/"

    def test_step_one_leads_to_the_accept_page_with_the_chosen_options(self):
        page = Client().get(f"/offert/{self.quote.token}/").content.decode()
        self.assertIn('method="get" action="' + self.url, page)
        r = Client().get(self.url, {"tillval": [self.foto.pk]})
        html = r.content.decode()
        self.assertContains(r, "Bekräfta beställningen")
        self.assertIn("Foto (tillval)", html)
        self.assertIn("59 500 kr", html)
        self.assertIn(f'name="tillval" value="{self.foto.pk}"', html)
        self.assertIn('value="info@nordan.se"', html)
        self.assertIn('value="Nordan Bygg AB"', html)
        self.quote.refresh_from_db()
        self.assertNotEqual(self.quote.status, QuoteStatus.ACCEPTED, "att titta accepterar inte")

    def test_missing_details_do_not_accept(self):
        r = Client().post(self.url, {"tillval": [self.foto.pk], "first_name": "Nina"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Något saknas")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.SENT)
        self.assertEqual(len(mail.outbox), 0)

    def test_org_number_is_validated_and_normalised(self):
        bad = dict(ACCEPT, org_number="123")
        r = Client().post(self.url, bad)
        self.assertContains(r, "tio siffror")
        Client().post(self.url, dict(ACCEPT, org_number="5567 12 3456"))
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.accept_org_number, "556712-3456")

    def test_accepting_stores_the_buyer_and_shows_the_receipt(self):
        r = Client().post(self.url, {**ACCEPT, "tillval": [self.foto.pk], "reference": "PO-77"})
        self.assertEqual(r["Location"], f"/offert/{self.quote.token}/")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.ACCEPTED)
        self.assertEqual(self.quote.accepted_by, "Nina Nordan, Nordan Bygg AB")
        self.assertEqual(self.quote.accept_reference, "PO-77")
        self.foto.refresh_from_db()
        self.assertTrue(self.foto.is_selected)
        html = Client().get(f"/offert/{self.quote.token}/").content.decode()
        self.assertIn("av Nina Nordan, Nordan Bygg AB", html)
        # Ett mejl, till byrån, med uppgifterna. Inget till kunden.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["staff@example.com"])
        self.assertIn("556712-3456", mail.outbox[0].body)
        self.assertIn("PO-77", mail.outbox[0].body)

    def test_unknown_confirm_box_blocks_the_order(self):
        data = dict(ACCEPT)
        data.pop("confirm")
        r = Client().post(self.url, data)
        self.assertContains(r, "Bekräfta att ni godkänner")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.SENT)

    def test_an_accepted_offer_sends_the_accept_page_back_to_the_receipt(self):
        Client().post(self.url, ACCEPT)
        self.assertEqual(Client().get(self.url)["Location"], f"/offert/{self.quote.token}/")

    def test_the_editor_shows_who_accepted(self):
        Client().post(self.url, dict(ACCEPT, message="Ring gärna på måndag."))
        user = get_user_model().objects.create_user("g", password="x", is_staff=True)
        client = Client()
        client.force_login(user)
        html = client.get(f"/manage/offerter/{self.quote.pk}/").content.decode()
        self.assertIn("Accepterad av", html)
        self.assertIn("556712-3456", html)
        self.assertIn("Ring gärna på måndag.", html)

    def test_creating_the_project_carries_the_buyer_details_to_the_customer(self):
        from apps.projects.models import Customer

        Client().post(self.url, ACCEPT)
        user = get_user_model().objects.create_user("g2", password="x", is_staff=True)
        client = Client()
        client.force_login(user)
        client.post(f"/manage/offerter/{self.quote.pk}/arenden/")
        customer = Customer.objects.get(name="Nordan Bygg AB")
        self.assertEqual(customer.org_number, "556712-3456")
        self.assertEqual(customer.email, "nina@nordan.se")
        self.assertEqual(customer.phone, "070-123 45 67")


@override_settings(**EMAIL_SETTINGS)
class AttachmentAndIpTests(TestCase):
    """Bilagor visas bara om de finns och hämtas mot token; IP:t syns vid bekräftelsen."""

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.staff = get_user_model().objects.create_user("g", password="x", is_staff=True)
        self.client_staff = Client()
        self.client_staff.force_login(self.staff)
        self.quote = Quote.objects.create(customer_name="Kund AB", status=QuoteStatus.SENT)
        QuoteLine.objects.create(quote=self.quote, label="Hemsida", price=1000, order=1)
        self.pdf = SimpleUploadedFile("info.pdf", b"%PDF-1.4 test", content_type="application/pdf")

    def test_no_attachments_means_no_section(self):
        html = Client().get(self.quote.get_public_url()).content.decode()
        self.assertNotIn("of-attachments", html)

    def test_upload_shows_a_button_on_the_offer_and_downloads_by_token(self):
        self.client_staff.post(f"/manage/offerter/{self.quote.pk}/bilaga/", {"files": self.pdf})
        attachment = self.quote.attachments.get()
        self.assertEqual(attachment.original_name, "info.pdf")
        html = Client().get(self.quote.get_public_url()).content.decode()
        self.assertIn("of-attachments", html)
        self.assertIn("info.pdf", html)
        url = f"/offert/{self.quote.token}/bilaga/{attachment.pk}/"
        r = Client().get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn("info.pdf", r["Content-Disposition"])
        self.assertNotIn("attachment", r["Content-Disposition"], "PDF öppnas i webbläsaren")
        other = Quote.objects.create(customer_name="Annan", status=QuoteStatus.SENT)
        self.assertEqual(
            Client().get(f"/offert/{other.token}/bilaga/{attachment.pk}/").status_code, 404
        )
        from django.conf import settings

        self.assertTrue(
            str(attachment.file.path).startswith(str(settings.PRIVATE_MEDIA_ROOT)),
            "bilagan ligger utanför /media/",
        )

    def test_disallowed_types_are_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("virus.exe", b"x", content_type="application/octet-stream")
        self.client_staff.post(f"/manage/offerter/{self.quote.pk}/bilaga/", {"files": bad})
        self.assertEqual(self.quote.attachments.count(), 0)

    def test_delete_and_lock_after_accept(self):
        self.client_staff.post(f"/manage/offerter/{self.quote.pk}/bilaga/", {"files": self.pdf})
        attachment = self.quote.attachments.get()
        Client().post(f"/offert/{self.quote.token}/acceptera/", ACCEPT)
        self.client_staff.post(f"/manage/offerter/bilaga/{attachment.pk}/ta-bort/")
        self.assertEqual(self.quote.attachments.count(), 1, "låst efter accept")
        from django.core.files.uploadedfile import SimpleUploadedFile

        more = SimpleUploadedFile("mer.pdf", b"%PDF", content_type="application/pdf")
        self.client_staff.post(f"/manage/offerter/{self.quote.pk}/bilaga/", {"files": more})
        self.assertEqual(self.quote.attachments.count(), 1)
        html = Client().get(self.quote.get_public_url()).content.decode()
        self.assertIn("info.pdf", html, "bilagan följer med kvittot")

    def test_delete_before_accept(self):
        self.client_staff.post(f"/manage/offerter/{self.quote.pk}/bilaga/", {"files": self.pdf})
        attachment = self.quote.attachments.get()
        self.client_staff.post(f"/manage/offerter/bilaga/{attachment.pk}/ta-bort/")
        self.assertEqual(self.quote.attachments.count(), 0)

    def test_the_customers_ip_is_shown_stored_and_receipted(self):
        url = f"/offert/{self.quote.token}/acceptera/"
        html = Client().get(url, HTTP_X_FORWARDED_FOR="203.0.113.5, 10.0.0.1").content.decode()
        self.assertIn("203.0.113.5", html)
        Client().post(url, ACCEPT, HTTP_X_FORWARDED_FOR="203.0.113.5")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.accepted_ip, "203.0.113.5")
        receipt = Client().get(self.quote.get_public_url()).content.decode()
        self.assertIn("(IP 203.0.113.5)", receipt)

    def test_garbage_forwarded_header_falls_back_to_remote_addr(self):
        from .public_views import client_ip

        class R:
            META = {"HTTP_X_FORWARDED_FOR": "not-an-ip", "REMOTE_ADDR": "198.51.100.7"}

        self.assertEqual(client_ip(R()), "198.51.100.7")
