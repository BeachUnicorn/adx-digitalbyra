"""ADX Flamingo, byråns sida: granskningskön, granskningen med diff och skäl,
publiceringen, Editor-filen, konverteringsexporten och Google-kopplingen."""

import csv
import io
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.http import QueryDict
from django.template.defaultfilters import date as date_filter
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.projects.models import Customer

from . import checks, exports, manage_review
from .models import (
    Campaign,
    ConversionUpload,
    Fact,
    FlamingoAccount,
    Lead,
    Review,
    Service,
)
from .testing import pages_from_campaigns

User = get_user_model()

HEADLINES = [
    "Badrumsrenovering i Nacka",
    "Nytt badrum i Nacka",
    "Begär offert i dag",
]
DESCRIPTIONS = [
    "Berätta om ditt badrum så återkommer vi med en offert.",
    "Rörjour i Nacka, Värmdö och Tyresö.",
]


class StaffFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            "elin", password="x12345678", is_staff=True, first_name="Elin"
        )
        cls.acme = Customer.objects.create(name="Lindqvist Rör AB")
        cls.other = Customer.objects.create(name="Hemlig Bygg AB")
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.acme.users.add(cls.anna)

        cls.account = FlamingoAccount.objects.create(
            customer=cls.acme,
            is_enabled=True,
            website_url="https://lindqvistror.se",
            google_status=FlamingoAccount.GOOGLE_BILLING_OK,
            google_ads_customer_id="123-456-7890",
        )
        cls.other_account = FlamingoAccount.objects.create(customer=cls.other, is_enabled=True)
        Fact.objects.create(
            account=cls.account,
            key="telefon",
            label="Telefon",
            value="08-000 00 00",
            source=Fact.SOURCE_SITE,
            confirmed=True,
        )
        Fact.objects.create(
            account=cls.account,
            key="behorighet",
            label="Behörighet",
            value="Säker Vatten-auktoriserade",
            source=Fact.SOURCE_SITE,
            confirmed=False,
        )
        cls.badrum = Service.objects.create(account=cls.account, name="Badrumsrenovering")
        cls.jour = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL
        )
        cls.tak = Service.objects.create(account=cls.other_account, name="Takbyte")

        cls.in_review = cls._campaign(cls.account, cls.badrum, "Badrum Nacka", "in_review")
        cls.review = Review.objects.create(
            campaign=cls.in_review,
            round=1,
            submitted_by=cls.anna,
            submitted_at=timezone.now() - timedelta(hours=3),
            snapshot=cls.in_review.content_snapshot(),
        )
        cls.approved = cls._campaign(cls.account, cls.jour, "Rörjour Nacka", "needs_customer")
        cls.approved.approved_at = timezone.now() - timedelta(hours=1)
        cls.approved.approved_by = cls.anna
        cls.approved.save()
        Review.objects.create(
            campaign=cls.approved,
            round=1,
            state=Review.STATE_DONE,
            reviewer=cls.staff,
            reviewed_at=timezone.now() - timedelta(hours=2),
        )
        cls.secret = cls._campaign(cls.other_account, cls.tak, "Hemlig kampanj", "in_review")
        Review.objects.create(
            campaign=cls.secret,
            round=1,
            submitted_at=timezone.now() - timedelta(days=2),
            snapshot=cls.secret.content_snapshot(),
        )
        pages_from_campaigns(cls.in_review, cls.approved, cls.secret)

    @staticmethod
    def _campaign(account, service, name, status):
        # Kampanjerna här har gått en granskningsrunda: kunden bad om den
        # (granskningen är kundens val, Campaign.review_requested).
        return Campaign.objects.create(
            account=account,
            service=service,
            name=name,
            status=status,
            review_requested=status != Campaign.STATUS_DRAFT,
            area="Nacka + 15 km",
            radius_km=15,
            daily_budget_kr=200,
            headlines=list(HEADLINES),
            descriptions=list(DESCRIPTIONS),
            keywords=[
                {"text": "badrumsrenovering nacka", "match": "phrase"},
                {"text": "renovera badrum", "match": "exact"},
            ],
            negatives=["jobb", "gratis", "gör det själv"],
            page={
                "title": "Badrumsrenovering i Nacka",
                "lead": "Berätta om ditt badrum.\nVi återkommer med en offert.",
                "points": ["Nacka, Värmdö och Tyresö"],
                "phone": "08-000 00 00",
                "form_title": "Berätta om ditt badrum",
                "questions": [
                    {"key": "storlek", "label": "Ungefär hur stort?", "kind": "text"},
                ],
                "note": "",
            },
        )

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def staff_client(self):
        return self.client_for(self.staff)


def form_data(campaign, **changes):
    """Granskningsformuläret som en webbläsare skickar det, med ändringar.

    Nycklar: headline_<n>, description_<n>, negatives ... som i formuläret;
    kw_text/kw_match som listor. Sidan rättas i sidbyggaren, inte här."""
    data = {}
    for i in range(15):
        data[f"headline_{i}"] = campaign.headlines[i] if i < len(campaign.headlines) else ""
    for i in range(4):
        data[f"description_{i}"] = (
            campaign.descriptions[i] if i < len(campaign.descriptions) else ""
        )
    data["kw_text"] = [k["text"] for k in campaign.keywords] + ["", "", ""]
    data["kw_match"] = [k["match"] for k in campaign.keywords] + ["phrase"] * 3
    data["negatives"] = "\r\n".join(campaign.negatives)
    data["note"] = ""
    data.update(changes)
    return data


def _query(data):
    """form_data som den QueryDict vyn får."""
    query = QueryDict(mutable=True)
    for key, value in data.items():
        if isinstance(value, list):
            query.setlist(key, value)
        else:
            query[key] = value
    return query


class NoChecks:
    """Kontrollerna (checks.py) skrivs av ett annat arbete. Här prövas
    granskningen själv; kontrollernas koppling prövas i ChecksTests."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(manage_review, "run_checks", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# Behörighet
# ---------------------------------------------------------------------------


class AccessTests(StaffFixture, TestCase):
    def urls(self):
        gets = [
            reverse("manage:flamingo_queue"),
            reverse("manage:flamingo_review", args=[self.in_review.pk]),
            reverse("manage:flamingo_review", args=[self.approved.pk]),
            reverse("manage:flamingo_editor_csv", args=[self.in_review.pk]),
            reverse("manage:flamingo_conversions_csv"),
        ]
        posts = [
            reverse("manage:flamingo_review", args=[self.in_review.pk]),
            reverse("manage:flamingo_publish", args=[self.approved.pk]),
            reverse("manage:flamingo_conversions_csv"),
            reverse("manage:flamingo_google_update", args=[self.acme.pk]),
        ]
        return gets, posts

    def test_everything_needs_staff_and_contacts_get_nothing(self):
        gets, posts = self.urls()
        anna = self.client_for(self.anna)
        for url in gets:
            with self.subTest(url=url, who="anonym"):
                response = Client().get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/manage/login/", response["Location"])
            with self.subTest(url=url, who="kontakt"):
                response = anna.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("/manage/", response["Location"])
                self.assertNotIn("text/csv", response.get("Content-Type", ""))
        for url in posts:
            with self.subTest(url=url, who="kontakt POST"):
                response = anna.post(url, {"action": "publish", "google_status": "linked"})
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("/manage/", response["Location"])

        # Ingenting ändrades av kontaktens försök.
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_BILLING_OK)

    def test_staff_reaches_every_page(self):
        gets, _posts = self.urls()
        client = self.staff_client()
        for url in gets + [
            reverse("manage:flamingo_overview"),
            reverse("manage:customer_detail", args=[self.acme.pk]),
        ]:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_every_view_is_wrapped_in_staff_required(self):
        """Varje vy i modulen går genom staff_required (som lägger på
        login_required): en ny vy utan dekoratorn fäller testet."""
        for name in (
            "queue",
            "review",
            "publish",
            "editor_csv",
            "conversions_csv",
            "google_update",
        ):
            view = getattr(manage_review, name)
            with self.subTest(view=name):
                source = Path(manage_review.__file__).read_text()
                pattern = rf"@staff_required\n(@[^\n]+\n)*def {name}\("
                self.assertRegex(source, pattern)
                self.assertTrue(callable(view))


# ---------------------------------------------------------------------------
# Kön
# ---------------------------------------------------------------------------


class QueueTests(NoChecks, StaffFixture, TestCase):
    def test_in_review_oldest_first_then_approved_not_live(self):
        response = self.staff_client().get(reverse("manage:flamingo_queue"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        to_review = html.split('id="att-granska"')[1].split('id="att-publicera"')[0]
        # Hemlig kampanj skickades in för två dagar sedan, Badrum för tre timmar sedan.
        self.assertLess(to_review.index("Hemlig kampanj"), to_review.index("Badrum Nacka"))
        self.assertNotIn("Rörjour Nacka", to_review)
        approved = html.split('id="att-publicera"')[1].split('id="hos-kunden"')[0]
        self.assertIn("Rörjour Nacka", approved)
        self.assertNotIn("Badrum Nacka", approved)

    def test_overview_counts_pending_reviews_and_links_to_the_queue(self):
        response = self.staff_client().get(reverse("manage:flamingo_overview"))
        self.assertContains(response, reverse("manage:flamingo_queue"))
        self.assertContains(response, reverse("manage:flamingo_queue") + "#konverteringar")
        self.assertContains(response, "Granska (2)")
        self.assertEqual(manage_review.queue_counts()["to_review"], 2)
        self.assertEqual(manage_review.queue_counts()["to_publish"], 1)

    def test_waiting_on_the_customer_shows_when_the_review_was_sent(self):
        """Kolumnen är när den senaste färdiga rundan gick till kunden, inte
        kampanjens senaste ändring."""
        sent = timezone.now() - timedelta(days=3)
        Campaign.objects.filter(pk=self.in_review.pk).update(status="needs_customer")
        Review.objects.filter(pk=self.review.pk).update(
            state=Review.STATE_DONE, reviewer=self.staff, reviewed_at=sent
        )
        html = self.staff_client().get(reverse("manage:flamingo_queue")).content.decode()
        waiting = html.split('id="hos-kunden"')[1].split('id="live"')[0]
        self.assertIn("<th>Till kunden</th>", waiting)
        self.assertNotIn("Hos kunden sedan", waiting)
        self.assertIn(date_filter(timezone.localtime(sent), "j b H:i"), waiting)
        item = manage_review.waiting_on_customer().get(pk=self.in_review.pk)
        self.assertEqual(item.sent_at, sent)

    def test_queue_tables_have_header_rows_for_the_phone_cards(self):
        html = self.staff_client().get(reverse("manage:flamingo_queue")).content.decode()
        for table in re.findall(r"<table.*?</table>", html, re.S):
            self.assertIn("<thead>", table)


# ---------------------------------------------------------------------------
# Granskningen
# ---------------------------------------------------------------------------


class ReviewTests(NoChecks, StaffFixture, TestCase):
    def url(self, campaign=None):
        return reverse("manage:flamingo_review", args=[(campaign or self.in_review).pk])

    def test_empty_text_slots_fold_away_after_two(self):
        """Tre rubriker: de tre och två tomma syns, de tio andra ligger i en
        utfällbar del och postas ändå."""
        html = self.staff_client().get(self.url()).content.decode()
        headlines = html.split('data-review-group="headlines"', 1)[1].split("</fieldset>", 1)[0]
        shown, more = headlines.split('<details class="mf-more">', 1)
        self.assertEqual(shown.count('name="headline_'), 5)
        self.assertEqual(more.count('name="headline_'), 10)
        self.assertIn("Fler rubriker", more)
        descriptions = html.split('data-review-group="descriptions"', 1)[1].split("</fieldset>", 1)[
            0
        ]
        self.assertEqual(descriptions.count('name="description_'), 4)
        self.assertNotIn("mf-more", descriptions)

    def test_split_slots_keeps_errors_and_filled_rows_visible(self):
        rows = [{"value": v, "error": ""} for v in ("a", "", "", "b", "", "", "", "")]
        shown, more = manage_review.split_slots(rows)
        self.assertEqual(len(shown), 6)
        self.assertEqual(len(more), 2)
        rows[7]["error"] = "Fel"
        self.assertEqual(manage_review.split_slots(rows)[1], [])
        self.assertEqual(
            manage_review.split_slots([{"value": ""}] * 4),
            ([{"value": ""}] * 2, [{"value": ""}] * 2),
        )

    def test_the_reviewer_sees_proposal_facts_and_website(self):
        response = self.staff_client().get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Badrumsrenovering i Nacka")
        self.assertContains(response, "https://lindqvistror.se")
        self.assertContains(response, "08-000 00 00")
        self.assertContains(response, "inte bekräftad")
        self.assertContains(response, "Klar, skicka till kunden")
        self.assertContains(response, 'name="reason_headlines"')

    def test_sending_without_changes_hands_over_to_the_customer(self):
        response = self.staff_client().post(self.url(), form_data(self.in_review))
        self.assertRedirects(response, reverse("manage:flamingo_queue"))
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, Review.STATE_DONE)
        self.assertEqual(self.review.changes, [])
        self.assertEqual(self.review.reviewer, self.staff)
        self.assertIsNotNone(self.review.reviewed_at)
        campaign = Campaign.objects.get(pk=self.in_review.pk)
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        # CRLF från webbläsaren är ingen ändring: ingressen är orörd.
        self.assertEqual(campaign.page["lead"], self.in_review.page["lead"])
        self.assertEqual(mail.outbox, [])

    def test_changes_are_stored_as_a_diff_with_the_reasons(self):
        data = form_data(
            self.in_review,
            headline_1="Badrum i Nacka | Fast offert",
            reason_headlines="Jouren gäller rörjour, inte renoveringar.",
            negatives="jobb\r\ngratis\r\ngör det själv\r\nbadrumsmatta",
            reason_negatives="De som söker mattor vill inte renovera.",
            kw_text=["badrumsrenovering nacka", "", "", "", ""],
            kw_match=["phrase", "exact", "phrase", "phrase", "phrase"],
            reason_keywords="För smalt.",
            # Sidan rättas i sidbyggaren, inte här: fältet läses inte längre.
            page_lead="Berätta om ditt badrum så återkommer vi med en offert.",
            note="Tre ändringar.",
        )
        response = self.staff_client().post(self.url(), data)
        self.assertRedirects(response, reverse("manage:flamingo_queue"))

        self.review.refresh_from_db()
        changes = self.review.changes
        self.assertIn(
            {
                "field": "headlines",
                "part": "headlines",
                "label": "Rubrik",
                "before": "Nytt badrum i Nacka",
                "after": "Badrum i Nacka | Fast offert",
                "reason": "Jouren gäller rörjour, inte renoveringar.",
            },
            changes,
        )
        self.assertIn(
            {
                "field": "negatives",
                "part": "negatives",
                "label": "Negativt sökord",
                "before": "",
                "after": "badrumsmatta",
                "reason": "De som söker mattor vill inte renovera.",
            },
            changes,
        )
        keyword = next(c for c in changes if c["part"] == "keywords")
        self.assertEqual(keyword["before"], "renovera badrum (exakt)")
        self.assertEqual(keyword["after"], "")
        self.assertFalse([c for c in changes if c["field"] == "page"])
        self.assertEqual(len(changes), 3)
        self.assertEqual(self.review.note, "Tre ändringar.")
        self.assertEqual(self.review.state, Review.STATE_DONE)
        self.assertEqual(self.review.reviewer, self.staff)

        campaign = Campaign.objects.get(pk=self.in_review.pk)
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIsNone(campaign.approved_at)
        self.assertEqual(campaign.headlines[1], "Badrum i Nacka | Fast offert")
        self.assertEqual(campaign.negatives[-1], "badrumsmatta")
        self.assertEqual(
            campaign.keywords, [{"text": "badrumsrenovering nacka", "match": "phrase"}]
        )
        # Sidan står kvar som den var: den rättas i sidbyggaren.
        self.assertEqual(campaign.landing_page.draft, self.in_review.landing_page.draft)
        self.assertEqual(campaign.page, self.in_review.page)
        self.assertEqual(mail.outbox, [])

    def test_a_changed_part_needs_a_reason(self):
        before = Campaign.objects.get(pk=self.in_review.pk)
        data = form_data(self.in_review, headline_0="Badrum i Nacka", page_title="Nytt badrum")
        response = self.staff_client().post(self.url(), data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Skriv varför du ändrade det", status_code=400)
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, Review.STATE_PENDING)
        campaign = Campaign.objects.get(pk=self.in_review.pk)
        self.assertEqual(campaign.headlines, before.headlines)
        self.assertEqual(campaign.status, Campaign.STATUS_IN_REVIEW)

    def test_every_changed_part_needs_its_own_reason(self):
        data = form_data(
            self.in_review,
            headline_0="Badrum i Nacka",
            reason_headlines="Kortare.",
            description_1="Rörjour i hela Nacka.",
        )
        response = self.staff_client().post(self.url(), data)
        self.assertEqual(response.status_code, 400)
        form = response.context["form"]
        self.assertIn("descriptions", form.reason_errors)
        self.assertNotIn("headlines", form.reason_errors)
        # Det postade står kvar i formuläret, och skälet med det.
        self.assertContains(response, 'value="Badrum i Nacka"', status_code=400)
        self.assertContains(response, 'value="Kortare."', status_code=400)
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, Review.STATE_PENDING)

    def test_google_limits_are_enforced(self):
        long = "En rubrik som är alldeles för lång"
        cases = [
            {"headline_0": long, "reason_headlines": "x"},
            {"headline_1": HEADLINES[0], "reason_headlines": "x"},
            {"headline_2": "", "headline_1": "", "reason_headlines": "x"},
            {"description_0": "x" * 91, "reason_descriptions": "x"},
            {"kw_text": ["", "", "", "", ""], "reason_keywords": "x"},
        ]
        for changes in cases:
            with self.subTest(changes=list(changes)):
                response = self.staff_client().post(
                    self.url(), form_data(self.in_review, **changes)
                )
                self.assertEqual(response.status_code, 400)
                self.review.refresh_from_db()
                self.assertEqual(self.review.state, Review.STATE_PENDING)

    def test_only_a_campaign_with_adx_can_be_edited(self):
        for campaign in (self.approved,):
            with self.subTest(campaign=campaign.name):
                data = form_data(campaign, headline_0="Ändrad", reason_headlines="x")
                response = self.staff_client().post(self.url(campaign), data)
                self.assertEqual(response.status_code, 302)
                campaign.refresh_from_db()
                self.assertEqual(campaign.headlines, HEADLINES)
        page = self.staff_client().get(self.url(self.approved))
        self.assertNotContains(page, "Klar, skicka till kunden")

    def test_a_campaign_in_review_without_a_round_gets_one(self):
        Review.objects.filter(campaign=self.in_review).delete()
        response = self.staff_client().post(self.url(), form_data(self.in_review))
        self.assertEqual(response.status_code, 302)
        review = Review.objects.get(campaign=self.in_review)
        self.assertEqual(review.state, Review.STATE_DONE)
        self.assertEqual(review.snapshot["headlines"], HEADLINES)

    def test_markup_in_the_form_is_stripped(self):
        data = form_data(
            self.in_review,
            headline_0="<b>Badrum</b> i Nacka",
            reason_headlines="<script>x</script>Kortare",
        )
        self.staff_client().post(self.url(), data)
        campaign = Campaign.objects.get(pk=self.in_review.pk)
        self.assertEqual(campaign.headlines[0], "Badrum i Nacka")
        change = Review.objects.get(pk=self.review.pk).changes[0]
        self.assertNotIn("<", change["reason"])

    def test_the_customer_sees_the_round_afterwards(self):
        """Efter granskningen visar sidan rundan (läsläge) och diffen."""
        data = form_data(self.in_review, headline_0="Badrum i Nacka", reason_headlines="Kortare.")
        self.staff_client().post(self.url(), data)
        response = self.staff_client().get(self.url())
        self.assertContains(response, "Senaste granskningen")
        self.assertContains(response, "<del>Badrumsrenovering i Nacka</del>", html=False)
        self.assertContains(response, "<ins>Badrum i Nacka</ins>", html=False)
        self.assertContains(response, "Kortare.")


class ChecksTests(StaffFixture, TestCase):
    def url(self):
        return reverse("manage:flamingo_review", args=[self.in_review.pk])

    def test_problems_from_the_checks_must_be_acknowledged(self):
        with mock.patch.object(
            manage_review, "run_checks", return_value=["Siffran 24 finns inte bland uppgifterna."]
        ):
            response = self.staff_client().post(self.url(), form_data(self.in_review))
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, "Siffran 24", status_code=400)
            self.assertContains(response, 'name="accept_checks"', status_code=400)
            self.review.refresh_from_db()
            self.assertEqual(self.review.state, Review.STATE_PENDING)

            data = form_data(self.in_review, accept_checks="1")
            response = self.staff_client().post(self.url(), data)
            self.assertEqual(response.status_code, 302)
            self.review.refresh_from_db()
            self.assertEqual(self.review.state, Review.STATE_DONE)

    def test_the_checks_become_lines_of_text(self):
        problems = [
            checks.Problem(field="headlines", message="Ordet billigast.", index=0),
            checks.Problem(
                field="page", message="Siffran 24 saknas.", part="lead", where="Hero, ingress"
            ),
            checks.Problem(field="", message="Något annat."),
        ]
        with mock.patch.object(checks, "validate", return_value=problems):
            self.assertEqual(
                manage_review.run_checks(self.in_review),
                [
                    "Rubrik 1: Ordet billigast.",
                    "Sidan, hero, ingress: Siffran 24 saknas.",
                    "Något annat.",
                ],
            )

    def test_a_broken_check_never_breaks_the_review(self):
        with (
            mock.patch.object(checks, "validate", side_effect=RuntimeError("trasig")),
            self.assertLogs("apps.flamingo.manage_review", level="ERROR"),
        ):
            self.assertIsNone(manage_review.run_checks(self.in_review))
        with mock.patch.object(checks, "validate", side_effect=RuntimeError("trasig")):
            response = self.staff_client().get(self.url())
        self.assertContains(response, "Inga automatiska kontroller kördes.")

    def test_problems_say_where_in_swedish(self):
        """checks.Problem har field, index och part; granskaren ser var."""
        cases = [
            (SimpleNamespace(field="headlines", message="För lång.", index=2, part=""), "Rubrik 3"),
            (SimpleNamespace(field="keywords", message="Krock.", index=0, part=""), "Sökord 1"),
            (
                SimpleNamespace(
                    field="page", message="Saknas.", index=None, part="phone", where="Hero, telefon"
                ),
                "Sidan, hero, telefon",
            ),
            (
                SimpleNamespace(
                    field="page", message="Siffra.", index=1, part="points", where="Hero, punkter 2"
                ),
                "Sidan, hero, punkter 2",
            ),
            (SimpleNamespace(field="area", message="Ange.", index=None, part=""), "Område"),
        ]
        for problem, where in cases:
            with self.subTest(where=where):
                self.assertTrue(manage_review._problem_text(problem).startswith(where))

    def test_the_real_checks_run_on_the_reviewers_version(self):
        """Med checks.py på plats: en siffra som ingen bekräftat stoppas tills
        granskaren bockat i att hen läst kontrollerna."""
        data = form_data(self.in_review, headline_0="Badrum från 9900 kr", reason_headlines="Pris.")
        problems = manage_review.run_checks(
            manage_review.ReviewForm.bound(self.in_review, _query(data)).proposal()
        )
        self.assertTrue(any("9900" in p or "9 900" in p for p in problems), problems)
        response = self.staff_client().post(self.url(), data)
        self.assertEqual(response.status_code, 400)
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, Review.STATE_PENDING)


# ---------------------------------------------------------------------------
# Publicering
# ---------------------------------------------------------------------------


class PublishTests(StaffFixture, TestCase):
    def post(self, campaign, **data):
        return self.staff_client().post(
            reverse("manage:flamingo_publish", args=[campaign.pk]), data
        )

    def test_nothing_goes_live_before_the_customer_approved(self):
        for campaign in (self.in_review,):
            self.post(campaign, action="publish")
            campaign.refresh_from_db()
            self.assertEqual(campaign.status, Campaign.STATUS_IN_REVIEW)
            self.assertIsNone(campaign.published_at)
        waiting = self._campaign(self.account, self.badrum, "Väntar", "needs_customer")
        self.post(waiting, action="publish")
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIsNone(waiting.published_at)

    def test_publish_after_approval_opens_the_landing_page(self):
        self.assertEqual(Client().get(self.approved.landing_url).status_code, 404)
        response = self.post(self.approved, action="publish", google_campaign_id="12 345 678")
        self.assertRedirects(
            response,
            reverse("manage:flamingo_review", args=[self.approved.pk]) + "#publicering",
            fetch_redirect_response=False,
        )
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, Campaign.STATUS_LIVE)
        self.assertIsNotNone(self.approved.published_at)
        self.assertEqual(self.approved.google_campaign_id, "12345678")
        self.assertEqual(mail.outbox, [])

    def test_publish_waits_for_the_google_link_but_not_for_billing(self):
        """Betalningen stoppar inte (beslut 2026-10-03), kopplingen gör det."""
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        self.post(self.approved, action="publish")
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, Campaign.STATUS_NEEDS_CUSTOMER)
        page = self.staff_client().get(reverse("manage:flamingo_review", args=[self.approved.pk]))
        self.assertContains(page, "Kan inte markeras som live")
        self.assertNotContains(page, "Markera som live</button>")
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_LINKED
        )
        page = self.staff_client().get(reverse("manage:flamingo_review", args=[self.approved.pk]))
        self.assertContains(page, "Annonserna visas först när kunden lagt in betalning")
        self.post(self.approved, action="publish")
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, Campaign.STATUS_LIVE)

    def test_a_new_round_blocks_publishing(self):
        Review.objects.create(campaign=self.approved, round=2)
        self.post(self.approved, action="publish")
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, Campaign.STATUS_NEEDS_CUSTOMER)

    def test_pause_and_resume(self):
        self.post(self.approved, action="publish")
        self.post(self.approved, action="pause")
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, Campaign.STATUS_PAUSED)
        self.assertEqual(Client().get(self.approved.landing_url).status_code, 404)
        published_at = self.approved.published_at
        self.post(self.approved, action="resume")
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, Campaign.STATUS_LIVE)
        self.assertEqual(self.approved.published_at, published_at)
        # Pausa något som inte är live, och återuppta något som inte är pausat: inget händer.
        self.post(self.in_review, action="pause")
        self.post(self.in_review, action="resume")
        self.in_review.refresh_from_db()
        self.assertEqual(self.in_review.status, Campaign.STATUS_IN_REVIEW)

    def test_the_page_shows_the_manual_steps_and_the_editor_file(self):
        response = self.staff_client().get(
            reverse("manage:flamingo_review", args=[self.approved.pk])
        )
        self.assertContains(response, "Google Ads API är inte inkopplat")
        self.assertContains(
            response, reverse("manage:flamingo_editor_csv", args=[self.approved.pk])
        )
        self.assertContains(response, "Markera som live")
        # Bara utvecklartoken räcker inte: API:t behöver hela kopplingen
        # (test_google_publish.py prövar vägen med API:t).
        with override_settings(GOOGLE_ADS_DEVELOPER_TOKEN="dev-token"):
            response = self.staff_client().get(
                reverse("manage:flamingo_review", args=[self.approved.pk])
            )
        self.assertContains(response, "Google Ads API är inte inkopplat")
        self.assertContains(response, "Markera som live</button>")

    def test_unknown_action_is_404(self):
        response = self.post(self.approved, action="radera")
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Filerna
# ---------------------------------------------------------------------------


def read_csv(text):
    return list(csv.reader(io.StringIO(text)))


class EditorCsvTests(StaffFixture, TestCase):
    def test_header_and_rows(self):
        text = exports.google_ads_editor_csv(self.approved)
        rows = read_csv(text)
        header = rows[0]
        self.assertEqual(
            text.splitlines()[0],
            "Campaign,Campaign Type,Networks,Campaign Daily Budget,Budget type,Languages,"
            "Bid Strategy Type,Campaign Status,Ad Group,Ad Group Status,Keyword,"
            "Criterion Type,Ad type,"
            + ",".join(f"Headline {n}" for n in range(1, 16))
            + ","
            + ",".join(f"Description {n}" for n in range(1, 5))
            + ",Final URL,Status,Comment",
        )
        records = [dict(zip(header, row, strict=True)) for row in rows[1:]]

        campaign = records[0]
        self.assertEqual(campaign["Campaign"], "Rörjour Nacka")
        self.assertEqual(campaign["Campaign Type"], "Search")
        self.assertEqual(campaign["Networks"], "Google Search")
        self.assertEqual(campaign["Campaign Daily Budget"], "200")
        self.assertEqual(campaign["Campaign Status"], "Paused")
        self.assertIn("Nacka + 15 km", campaign["Comment"])
        self.assertIn("15 km", campaign["Comment"])

        group = records[1]
        self.assertEqual(group["Ad Group"], "Rörjour")

        keywords = {(r["Keyword"], r["Criterion Type"]) for r in records if r["Keyword"]}
        self.assertEqual(
            keywords,
            {
                ("badrumsrenovering nacka", "Phrase"),
                ("renovera badrum", "Exact"),
                ("jobb", "Negative Broad"),
                ("gratis", "Negative Broad"),
                ("gör det själv", "Negative Broad"),
            },
        )
        ad = records[-1]
        self.assertEqual(ad["Ad type"], "Responsive search ad")
        self.assertEqual(ad["Headline 1"], HEADLINES[0])
        self.assertEqual(ad["Headline 3"], HEADLINES[2])
        self.assertEqual(ad["Headline 4"], "")
        self.assertEqual(ad["Description 2"], DESCRIPTIONS[1])
        self.assertEqual(ad["Final URL"], f"https://adx.se/lp/{self.approved.page_slug}/")

    def test_the_download_is_an_attachment_with_a_bom(self):
        response = self.staff_client().get(
            reverse("manage:flamingo_editor_csv", args=[self.approved.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment;", response["Content-Disposition"])
        body = response.content.decode("utf-8")
        self.assertTrue(body.startswith("﻿Campaign,"))

    def test_formula_cells_are_neutralised(self):
        Campaign.objects.filter(pk=self.approved.pk).update(
            headlines=['=HYPERLINK("http://x")', "+46 8 000", "-1", "@SUM(A1)", "\tTabb"],
            keywords=[{"text": "=cmd|' /C calc'!A0", "match": "phrase"}],
        )
        self.approved.refresh_from_db()
        rows = read_csv(exports.google_ads_editor_csv(self.approved))
        header, ad = rows[0], rows[-1]
        record = dict(zip(header, ad, strict=True))
        self.assertEqual(record["Headline 1"], '\'=HYPERLINK("http://x")')
        self.assertEqual(record["Headline 2"], "'+46 8 000")
        self.assertEqual(record["Headline 3"], "'-1")
        self.assertEqual(record["Headline 4"], "'@SUM(A1)")
        self.assertEqual(record["Headline 5"], "'\tTabb")
        keyword = next(r for r in rows[1:] if dict(zip(header, r, strict=True))["Keyword"])
        self.assertTrue(dict(zip(header, keyword, strict=True))["Keyword"].startswith("'="))

    def test_safe_cell(self):
        for raw, safe in [
            ("=1+1", "'=1+1"),
            ("+1", "'+1"),
            ("-1", "'-1"),
            ("@x", "'@x"),
            ("\tx", "'\tx"),
            ("\rx", "'\rx"),
            ("Badrum", "Badrum"),
            ("", ""),
            (None, ""),
            (186000, "186000"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(exports.safe_cell(raw), safe)


class ConversionCsvTests(StaffFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.won = Lead.objects.create(
            account=cls.account,
            name="Sara Holm",
            gclid="Cj0KCQjw-abc_123",
            ad_consent=Lead.CONSENT_GRANTED,
        )
        cls.won.set_status(
            Lead.STATUS_WON, value_kr=186000, now=datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
        )
        cls.other_won = Lead.objects.create(
            account=cls.other_account,
            name="Bo",
            gclid="EAIaIQobChMI",
            ad_consent=Lead.CONSENT_GRANTED,
        )
        cls.other_won.set_status(
            Lead.STATUS_WON, value_kr=45000, now=datetime(2026, 1, 15, 8, 30, tzinfo=UTC)
        )
        cls.no_click = Lead.objects.create(account=cls.account, name="Utan klick")
        cls.no_click.set_status(Lead.STATUS_WON, value_kr=1000)

    def test_google_offline_conversion_format(self):
        uploads = manage_review.queued_uploads()
        text = exports.offline_conversions_csv(uploads)
        lines = text.splitlines()
        self.assertEqual(lines[0], "Parameters:TimeZone=Europe/Stockholm")
        self.assertEqual(
            lines[1],
            "Google Click ID,Conversion Name,Conversion Time,Conversion Value,"
            "Conversion Currency,Ad User Data",
        )
        self.assertEqual(
            sorted(lines[2:]),
            sorted(
                [
                    # 10:00 UTC är 12:00 i svensk sommartid, 08:30 UTC 09:30 på vintern.
                    "Cj0KCQjw-abc_123,ADX Flamingo affär,2026-10-01 12:00:00,186000,SEK,Granted",
                    "EAIaIQobChMI,ADX Flamingo affär,2026-01-15 09:30:00,45000,SEK,Granted",
                ]
            ),
        )

    @override_settings(FLAMINGO_CONVERSION_NAME="Affär (offline)")
    def test_the_conversion_name_comes_from_settings(self):
        text = exports.offline_conversions_csv(manage_review.queued_uploads())
        self.assertIn(",Affär (offline),", text)

    def test_click_ids_and_values_are_escaped_like_every_cell(self):
        Lead.objects.filter(pk=self.won.pk).update(gclid="=HYPERLINK(1)")
        text = exports.offline_conversions_csv(
            manage_review.queued_uploads(customer_pk=self.acme.pk)
        )
        self.assertIn("'=HYPERLINK(1),", text)

    def test_download_filters_by_customer_and_only_takes_its_rows_from_the_api(self):
        client = self.staff_client()
        url = reverse("manage:flamingo_conversions_csv")
        response = client.get(url, {"kund": self.other.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        body = response.content.decode()
        self.assertTrue(body.startswith("Parameters:TimeZone=Europe/Stockholm"))
        self.assertIn("EAIaIQobChMI", body)
        self.assertNotIn("Cj0KCQjw", body)
        # Utan klick-id köades ingen konvertering alls (Lead.set_status).
        self.assertFalse(ConversionUpload.objects.filter(lead=self.no_click).exists())
        self.assertEqual(
            ConversionUpload.objects.filter(status=ConversionUpload.STATUS_QUEUED).count(), 2
        )
        # Raden i filen skickas inte med API:t; den andra kundens rad är orörd.
        self.assertIsNotNone(ConversionUpload.objects.get(lead=self.other_won).downloaded_at)
        self.assertIsNone(ConversionUpload.objects.get(lead=self.won).downloaded_at)
        self.assertEqual(client.get(url, {"kund": "abc"}).status_code, 404)
        self.assertEqual(client.get(url, {"kund": 999999}).status_code, 404)

    def test_mark_as_exported_only_touches_the_rows_in_the_file(self):
        client = self.staff_client()
        url = reverse("manage:flamingo_conversions_csv")
        mine = ConversionUpload.objects.get(lead=self.won)
        theirs = ConversionUpload.objects.get(lead=self.other_won)
        # Utan nedladdning markeras ingenting: raden har inte varit i en fil.
        response = client.post(url, {"kund": self.acme.pk, "upload": [mine.pk]}, follow=True)
        self.assertContains(response, "har inte varit med i en nedladdad fil")
        mine.refresh_from_db()
        self.assertEqual(mine.status, ConversionUpload.STATUS_QUEUED)
        client.get(url, {"kund": self.acme.pk})
        client.get(url, {"kund": self.other.pk})
        # Formuläret för Lindqvist, men någon har lagt till Hemligs rad i POST:en.
        response = client.post(url, {"kund": self.acme.pk, "upload": [mine.pk, theirs.pk]})
        self.assertRedirects(
            response,
            reverse("manage:flamingo_queue") + "#konverteringar",
            fetch_redirect_response=False,
        )
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.status, ConversionUpload.STATUS_EXPORTED)
        self.assertIsNotNone(mine.exported_at)
        self.assertEqual(mine.response["by"], "elin")
        self.assertEqual(theirs.status, ConversionUpload.STATUS_QUEUED)
        # En rad som redan är exporterad markeras inte om.
        exported_at = mine.exported_at
        client.post(url, {"upload": [mine.pk]})
        mine.refresh_from_db()
        self.assertEqual(mine.exported_at, exported_at)
        # En rad vars klick-id försvunnit kom aldrig med i filen och markeras inte.
        Lead.objects.filter(pk=self.other_won.pk).update(gclid="")
        client.post(url, {"upload": [theirs.pk]})
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, ConversionUpload.STATUS_QUEUED)

    def test_the_queue_page_lists_conversions_per_customer(self):
        response = self.staff_client().get(reverse("manage:flamingo_queue"))
        self.assertContains(response, 'id="konverteringar"')
        self.assertContains(response, f"?kund={self.acme.pk}")
        self.assertContains(response, "Sara Holm")
        self.assertContains(response, "Markera som exporterade")


# ---------------------------------------------------------------------------
# Google-kopplingen
# ---------------------------------------------------------------------------


class GoogleUpdateTests(StaffFixture, TestCase):
    def post(self, **data):
        return self.staff_client().post(
            reverse("manage:flamingo_google_update", args=[self.other.pk]), data
        )

    def test_the_id_is_ten_digits_stored_with_dashes(self):
        for raw in ("2223334444", "222 333 4444", "222-333-4444"):
            with self.subTest(raw=raw):
                response = self.post(
                    google_status="linked", google_ads_customer_id=raw, google_note="Kopplat"
                )
                self.assertRedirects(
                    response,
                    reverse("manage:customer_detail", args=[self.other.pk]) + "#flamingo",
                    fetch_redirect_response=False,
                )
                self.other_account.refresh_from_db()
                self.assertEqual(self.other_account.google_ads_customer_id, "222-333-4444")
                self.assertEqual(self.other_account.google_status, "linked")
                self.assertEqual(self.other_account.google_note, "Kopplat")
        self.assertEqual(mail.outbox, [])

    def test_invalid_input_saves_nothing(self):
        for data in (
            {"google_status": "linked", "google_ads_customer_id": "12345"},
            {"google_status": "linked", "google_ads_customer_id": "123456789012"},
            {"google_status": "hackad", "google_ads_customer_id": "1234567890"},
            {"google_status": "billing_ok", "google_ads_customer_id": ""},
            {},
        ):
            with self.subTest(data=data):
                self.post(**data)
                self.other_account.refresh_from_db()
                self.assertEqual(self.other_account.google_status, "not_started")
                self.assertEqual(self.other_account.google_ads_customer_id, "")

    def test_an_id_another_customer_has_is_refused(self):
        # Acme har 123-456-7890. Ett Google Ads-konto hör till en kund.
        response = self.post(google_status="linked", google_ads_customer_id="1234567890")
        self.assertEqual(response.status_code, 302)
        self.other_account.refresh_from_db()
        self.assertEqual(self.other_account.google_ads_customer_id, "")
        self.assertEqual(self.other_account.google_status, "not_started")
        text = " ".join(str(m) for m in get_messages(response.wsgi_request))
        self.assertIn("används redan av Lindqvist Rör AB", text)

    def test_a_new_id_clears_old_google_errors_on_unpublished_campaigns(self):
        campaign = self.approved
        Campaign.objects.filter(pk=campaign.pk).update(google_error="Kontots valuta är EUR.")
        self.staff_client().post(
            reverse("manage:flamingo_google_update", args=[self.acme.pk]),
            {"google_status": "id_given", "google_ads_customer_id": "999-888-7777"},
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.google_error, "")

    def test_the_id_can_be_cleared_while_not_linked(self):
        FlamingoAccount.objects.filter(pk=self.other_account.pk).update(
            google_ads_customer_id="222-333-4444", google_status="id_given"
        )
        self.post(google_status="requested_new", google_ads_customer_id="")
        self.other_account.refresh_from_db()
        self.assertEqual(self.other_account.google_ads_customer_id, "")
        self.assertEqual(self.other_account.google_status, "requested_new")

    def test_the_note_is_plain_text(self):
        self.post(
            google_status="requested_new",
            google_ads_customer_id="",
            google_note="<b>Ringde</b> kunden",
        )
        self.other_account.refresh_from_db()
        self.assertEqual(self.other_account.google_note, "Ringde kunden")

    def test_the_customer_card_shows_the_google_form_and_campaigns(self):
        response = self.staff_client().get(reverse("manage:customer_detail", args=[self.acme.pk]))
        self.assertContains(response, reverse("manage:flamingo_google_update", args=[self.acme.pk]))
        self.assertContains(response, 'value="123-456-7890"')
        self.assertContains(response, "Badrum Nacka")
        self.assertContains(response, "Att granska")
        self.assertContains(response, "Godkänd, ej publicerad")

    def test_a_customer_without_flamingo_has_no_google_form(self):
        plain = Customer.objects.create(name="Utan Flamingo AB")
        response = self.staff_client().get(reverse("manage:customer_detail", args=[plain.pk]))
        self.assertNotContains(response, reverse("manage:flamingo_google_update", args=[plain.pk]))
        response = self.staff_client().post(
            reverse("manage:flamingo_google_update", args=[plain.pk]),
            {"google_status": "linked", "google_ads_customer_id": "1234567890"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(FlamingoAccount.objects.filter(customer=plain).exists())


# ---------------------------------------------------------------------------
# Mallarna
# ---------------------------------------------------------------------------


class CustomerPanelTextTests(TestCase):
    def test_the_flamingo_panel_text_is_16px(self):
        # Löptexten i kundkortets Flamingo-panel (bland den vad Google mejlar)
        # är 16 px; resten av kundkortet behåller sina små noteringar.
        base = Path(settings.BASE_DIR)
        panel = (base / "templates" / "flamingo" / "_customer_panel.html").read_text("utf-8")
        self.assertIn('class="m-panel tv-panel tv-panel--read" id="flamingo"', panel)
        css = (base / "static" / "css" / "tavla.css").read_text("utf-8")
        self.assertRegex(
            css,
            r"\.tv-panel--read \.tv-prop__note,\.tv-panel--read \.tv-checklabel--box"
            r"\{font-size:16px",
        )


class TemplateGuardTests(TestCase):
    """Inga style-attribut och inga inbäddade skript i byråns Flamingo-mallar
    (CSS i static/css/manage-flamingo.css, JS i static/js/manage-flamingo.js)."""

    def test_no_inline_style_or_script(self):
        base = Path(settings.BASE_DIR) / "templates"
        paths = list((base / "manage" / "flamingo").glob("*.html")) + [
            base / "flamingo" / "_customer_panel.html"
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("style=", text)
                self.assertNotRegex(text, r"<script(?![^>]*\bsrc=)[^>]*>")
                self.assertNotIn("[PLACEHOLDER", text)
