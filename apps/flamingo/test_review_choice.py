"""
Granskningen är kundens val (beslut 2026-10-03): kryssrutan vid inskicket,
publiceringen direkt efter kundens godkännande (google_publish.publish_approved),
byråns kö och larm, Google-steget när ADX skickat kopplingsförfrågan, och
demokunden som inte blandar sig med byråns arbete.

Inget här når nätet: urlopen byts mot FakeGoogle (test_google_ads.py), som
fäller testet vid ett anrop som inte köats.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import google_publish, manage_review, places, rules
from .models import Campaign, FlamingoAccount, Review
from .test_google_ads import CONFIGURED, NOTHING, TOKEN_OK
from .test_google_publish import CID, PublishFixture, created, customer_row

AGENCY = "byran@example.com"


def messages_of(response):
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


class ChoiceFixture(PublishFixture):
    """PublishFixture med kampanjen som ett utkast utan granskning, som
    kunden (Anna) skickar in från verktyget. Byråns larm går till AGENCY."""

    def setUp(self):
        super().setUp()
        agency = override_settings(INQUIRY_NOTIFICATION_EMAIL=AGENCY)
        agency.enable()
        self.addCleanup(agency.disable)
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.STATUS_DRAFT, approved_at=None, approved_by=None
        )
        self.campaign.reviews.all().delete()
        self.reload()

    def customer_client(self):
        client = Client()
        client.force_login(self.anna)
        return client

    def submit(self, **data):
        return self.customer_client().post(
            reverse("flamingo:app_campaign_submit", args=[self.campaign.pk]), data
        )

    def detail(self, tab=""):
        url = reverse("flamingo:app_campaign", args=[self.campaign.pk])
        return self.customer_client().get(f"{url}?flik={tab}" if tab else url)

    def queue_html(self):
        return self.staff_client().get(reverse("manage:flamingo_queue")).content.decode()


# ---------------------------------------------------------------------------
# Kryssrutan
# ---------------------------------------------------------------------------


class SubmitFormTests(ChoiceFixture, TestCase):
    def test_the_choice_is_offered_unticked(self):
        with override_settings(**NOTHING):
            page = self.detail()
        self.assertContains(page, "Jag vill att ADX granskar kampanjen innan den publiceras")
        self.assertContains(page, '<input type="checkbox" name="review" value="1">', html=False)
        self.assertNotContains(page, 'name="review" value="1" checked')
        # Båda vägarna står vid knappen.
        self.assertContains(page, "Skicka till granskning")
        self.assertContains(page, "du godkänner ändringarna innan den publiceras")
        self.assertContains(page, "Google granskar också varje annons innan den visas.")

    def test_the_text_says_what_happens_without_review(self):
        with override_settings(**CONFIGURED):
            page = self.detail()
        self.assertContains(page, "Kampanjen publiceras direkt, eftersom kontrollerna är gröna.")
        self.assertContains(page, '<span class="fl-camp-submit__direct">Publicera</span>')
        with override_settings(**NOTHING):
            page = self.detail()
        self.assertContains(page, "ADX publicerar kampanjen som den är, utan att granska den.")
        self.assertContains(page, "Skicka för publicering")
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        with override_settings(**CONFIGURED):
            page = self.detail()
        self.assertContains(
            page, "Kampanjen publiceras som den är när ditt Google Ads-konto är kopplat under ADX."
        )

    def test_not_linked_is_what_stops_going_live_not_billing(self):
        with override_settings(**NOTHING):
            page = self.detail()
        # Kopplat, betalningen saknas: ingen varning om att den stoppar.
        self.assertNotContains(page, "betalningen är klar")
        self.assertNotContains(page, "Kampanjen kan gå live först när")
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        with override_settings(**NOTHING):
            page = self.detail()
        self.assertContains(
            page, "Kampanjen kan gå live först när ditt Google Ads-konto är kopplat under ADX."
        )


# ---------------------------------------------------------------------------
# Utan granskning
# ---------------------------------------------------------------------------


class WithoutReviewTests(ChoiceFixture, TestCase):
    @override_settings(**CONFIGURED)
    def test_with_the_api_it_goes_live_at_once(self):
        ops = google_publish.build_operations(self.campaign, CID)
        fake = self.google(TOKEN_OK, customer_row(auto_tagging=True), created(ops))
        response = self.submit()
        self.assertRedirects(
            response,
            reverse("flamingo:app_campaign", args=[self.campaign.pk]) + "?flik=granskning",
            fetch_redirect_response=False,
        )
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertFalse(campaign.review_requested)
        self.assertEqual(campaign.approved_by, self.anna)
        self.assertIsNotNone(campaign.approved_at)
        self.assertEqual(campaign.google_campaign_id, "555")
        self.assertFalse(campaign.reviews.exists())
        self.assertEqual(len(fake.requests), 3)
        text = messages_of(response)
        self.assertIn("Kampanjen är live.", text)
        self.assertIn("Annonserna visas när betalningen är inlagd hos Google.", text)
        self.assertEqual(Client().get(campaign.landing_url).status_code, 200)
        # Byrån larmas med vad som hände; kunden mejlas inte.
        self.assertEqual(len(mail.outbox), 1)
        alert = mail.outbox[0]
        self.assertEqual(alert.to, [AGENCY])
        self.assertIn("är live", alert.subject)
        self.assertIn("utan att be om granskning", alert.body)
        self.assertIn("live hos Google (kampanj 555)", alert.body)
        self.assertNotIn(campaign.pk, [c.pk for c in manage_review.approved_not_live()])
        self.assertNotIn(campaign.pk, [i.campaign.pk for i in manage_review.to_review_items()])
        page = self.detail("granskning")
        self.assertContains(page, "Skickad utan granskning")
        self.assertContains(page, "Kontrollerna gick igenom")

    @override_settings(**NOTHING)
    def test_without_the_api_it_waits_for_adx_in_the_queue(self):
        fake = self.google()  # inget anrop alls
        response = self.submit()
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertEqual(campaign.approved_by, self.anna)
        self.assertIsNone(campaign.published_at)
        self.assertFalse(campaign.reviews.exists())
        self.assertEqual(fake.requests, [])
        self.assertIn(
            "ADX publicerar kampanjen, och du ser här när den är live.", messages_of(response)
        )
        self.assertIn(campaign.pk, [c.pk for c in manage_review.approved_not_live()])
        self.assertEqual(manage_review.to_review_items(), [])
        alert = mail.outbox[0]
        self.assertIn("publicera", alert.subject)
        self.assertIn("Google Ads API är inte inkopplat", alert.body)
        # Kunden ser ett lugnt läge, utan tider.
        page = self.detail()
        self.assertContains(page, "Skickad av dig, ADX publicerar")
        self.assertContains(page, "Skickad av dig ")
        self.assertNotContains(page, "Godkänd av dig")
        # Byrån ser vägen och varför den inte är live.
        html = self.queue_html()
        to_publish = html.split('id="att-publicera"')[1].split('id="hos-kunden"')[0]
        self.assertIn("Rörjour Nacka", to_publish)
        self.assertIn("utan granskning", to_publish)
        self.assertIn("Google Ads API är inte inkopplat: publicera för hand.", to_publish)
        review = self.review_page()
        self.assertContains(review, "vid inskicket utan granskning")
        self.assertContains(review, "Markera som live")

    @override_settings(**CONFIGURED)
    def test_not_linked_waits_for_the_link(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        fake = self.google()
        response = self.submit()
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIsNotNone(campaign.approved_at)
        self.assertEqual(fake.requests, [])
        self.assertIn("när ditt Google Ads-konto är kopplat under ADX", messages_of(response))
        self.assertIn("inte kopplat under ADX", mail.outbox[0].body)
        to_publish = self.queue_html().split('id="att-publicera"')[1]
        self.assertIn("Kontot är inte kopplat under ADX.", to_publish)

    @override_settings(**CONFIGURED)
    def test_a_refusal_from_google_leaves_it_approved_with_the_reason(self):
        self.google(TOKEN_OK, customer_row(currency="EUR"))
        response = self.submit()
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIsNotNone(campaign.approved_at)
        self.assertIsNone(campaign.published_at)
        self.assertIn("EUR", campaign.google_error)
        # Kunden ser inget fel, bara att ADX publicerar.
        text = messages_of(response)
        self.assertIn("ADX publicerar kampanjen", text)
        self.assertNotIn("EUR", text)
        self.assertNotContains(self.detail(), "EUR")
        # Byrån får orsaken i larmet och i kön.
        self.assertIn("EUR", mail.outbox[0].body)
        self.assertIn("EUR", self.queue_html().split('id="att-publicera"')[1])

    @override_settings(**CONFIGURED)
    def test_a_refusal_before_google_is_stored_for_the_queue(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(area="")
        self.reload()
        with mock.patch("apps.flamingo.checks.validate", return_value=[]):
            fake = self.google()
            self.submit()
        campaign = self.reload()
        self.assertEqual(fake.requests, [])
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertEqual(campaign.google_error, google_publish.MSG_NO_PLACES)

    @override_settings(**CONFIGURED)
    def test_an_unexpected_error_never_fails_the_customer(self):
        with mock.patch.object(google_publish, "go_live", side_effect=RuntimeError("trasig")):
            with self.assertLogs("apps.flamingo.google_publish", "ERROR"):
                response = self.submit()
        self.assertEqual(response.status_code, 302)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertEqual(campaign.google_error, google_publish.MSG_UNEXPECTED)
        self.assertNotIn("trasig", messages_of(response))

    @override_settings(**CONFIGURED)
    def test_the_demo_never_calls_google_and_sends_nothing(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        fake = self.google()
        self.submit()
        campaign = self.reload()
        self.assertEqual(fake.requests, [])
        self.assertEqual(mail.outbox, [])
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIsNotNone(campaign.approved_at)


# ---------------------------------------------------------------------------
# Med granskning
# ---------------------------------------------------------------------------


class WithReviewTests(ChoiceFixture, TestCase):
    @override_settings(**CONFIGURED)
    def test_ticked_goes_to_adx_as_before(self):
        fake = self.google()
        response = self.submit(review="1")
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_IN_REVIEW)
        self.assertTrue(campaign.review_requested)
        self.assertIsNone(campaign.approved_at)
        review = campaign.reviews.get()
        self.assertEqual((review.round, review.state), (1, Review.STATE_PENDING))
        self.assertEqual(fake.requests, [])
        self.assertIn("Skickat till granskning", messages_of(response))
        alert = mail.outbox[0]
        self.assertIn("granska", alert.subject)
        self.assertIn("bett ADX granska den", alert.body)
        items = manage_review.to_review_items()
        self.assertEqual([item.campaign.pk for item in items], [campaign.pk])
        review_page = self.review_page()
        self.assertContains(review_page, "Kunden bad om granskning")

    def reviewed(self):
        """ADX har granskat klart och lämnat över till kunden."""
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.STATUS_NEEDS_CUSTOMER, review_requested=True
        )
        Review.objects.create(
            campaign=self.campaign,
            round=1,
            state=Review.STATE_DONE,
            reviewer=self.staff,
            reviewed_at=timezone.now() - timedelta(hours=1),
        )
        return reverse("flamingo:app_campaign_approve", args=[self.campaign.pk])

    @override_settings(**CONFIGURED)
    def test_the_approval_goes_live_at_once_with_the_api(self):
        approve = self.reviewed()
        ops = google_publish.build_operations(self.reload(), CID)
        self.google(TOKEN_OK, customer_row(auto_tagging=True), created(ops))
        response = self.customer_client().post(approve)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertEqual(campaign.approved_by, self.anna)
        self.assertIn("Godkänd. Kampanjen är live.", messages_of(response))
        self.assertIn("är live", mail.outbox[0].subject)
        self.assertIn("efter runda 1", mail.outbox[0].body)

    @override_settings(**NOTHING)
    def test_the_approval_without_the_api_goes_to_the_queue(self):
        approve = self.reviewed()
        fake = self.google()
        response = self.customer_client().post(approve)
        campaign = self.reload()
        self.assertEqual(fake.requests, [])
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIsNotNone(campaign.approved_at)
        self.assertIn(campaign.pk, [c.pk for c in manage_review.approved_not_live()])
        self.assertIn("ADX publicerar kampanjen", messages_of(response))
        self.assertContains(self.detail(), "Godkänd av dig, ADX publicerar")
        self.assertIn("kunden bad om granskning", self.queue_html())


# ---------------------------------------------------------------------------
# Kön: bara de som bad om granskning, och demokunden för sig
# ---------------------------------------------------------------------------


@override_settings(**NOTHING)
class QueueTests(ChoiceFixture, TestCase):
    def test_to_review_only_has_campaigns_that_asked_for_it(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(status=Campaign.STATUS_IN_REVIEW)
        self.assertEqual(manage_review.to_review_items(), [])
        Campaign.objects.filter(pk=self.campaign.pk).update(review_requested=True)
        self.assertEqual(len(manage_review.to_review_items()), 1)

    def test_the_demo_stays_out_of_the_queue_unless_asked_for(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.STATUS_IN_REVIEW, review_requested=True
        )
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.assertEqual(manage_review.to_review_items(), [])
        self.assertEqual(manage_review.queue_counts()["to_review"], 0)
        html = self.queue_html()
        self.assertNotIn("Rörjour Nacka", html.split('id="att-granska"')[1])
        self.assertIn("Visa demokunden", html)
        client = self.staff_client()
        shown = client.get(reverse("manage:flamingo_queue") + "?demo=1").content.decode()
        self.assertIn("Rörjour Nacka", shown.split('id="att-granska"')[1])
        self.assertIn("Dölj demokunden", shown)
        overview = client.get(reverse("manage:flamingo_overview")).content.decode()
        self.assertIn("Demo", overview)
        self.assertNotIn("Granska (1)", overview)
        # Demokundens kampanj går att öppna, och kortet länkar dit.
        review = self.review_page()
        self.assertContains(review, reverse("manage:flamingo_queue") + "?demo=1")


# ---------------------------------------------------------------------------
# Google-steget när ADX skickat kopplingsförfrågan
# ---------------------------------------------------------------------------


class GoogleStepTests(ChoiceFixture, TestCase):
    def test_a_sent_link_request_waits_on_the_customer_not_adx(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        self.account.refresh_from_db()
        self.assertTrue(self.account.google_waiting_on_adx)
        step = rules.onboarding_for(self.account).steps[2]
        self.assertEqual(step.state, rules.STEP_WAIT)

        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_link_requested_at=timezone.now()
        )
        self.account.refresh_from_db()
        self.assertFalse(self.account.google_waiting_on_adx)
        self.assertTrue(self.account.google_waiting_on_customer)
        step = rules.onboarding_for(self.account).steps[2]
        self.assertNotEqual(step.state, rules.STEP_WAIT)
        self.assertEqual(step.note, rules.GOOGLE_ACCEPT_NOTE)
        keys = [thing.key for thing in rules.three_things(self.account)]
        self.assertIn("google_accept", keys)

    def test_linked_is_done_without_billing(self):
        self.assertTrue(self.account.google_linked)
        self.assertFalse(self.account.google_ready)
        step = rules.onboarding_for(self.account).steps[2]
        self.assertEqual(step.state, rules.STEP_DONE)


class PlacesDemoTests(ChoiceFixture, TestCase):
    @override_settings(GOOGLE_PLACES_API_KEY="test-places-key")
    def test_places_never_asks_google_about_the_demo(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        with mock.patch.object(places, "search", side_effect=AssertionError("anrop")):
            self.assertIsNone(places.update_from_google(self.account))


# ---------------------------------------------------------------------------
# Efter ett nej: spärrar mot en slinga, gamla fel, vägen för hand och
# tillbaka till granskning
# ---------------------------------------------------------------------------


class AfterRefusalTests(ChoiceFixture, TestCase):
    def reopen(self):
        """Kunden ändrar något: kampanjen blir ett utkast igen (_reopen)."""
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.STATUS_DRAFT, approved_at=None, approved_by=None
        )
        self.reload()

    @override_settings(**CONFIGURED)
    def test_a_resubmit_loop_does_not_call_google_or_mail_adx_again(self):
        fake = self.google(TOKEN_OK, customer_row(currency="EUR"))
        self.submit()
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(len(mail.outbox), 1)
        for _ in range(5):
            self.reopen()
            response = self.submit()
            self.assertEqual(response.status_code, 302)
        # Inga fler anrop (FakeGoogle fäller testet vid ett oköat anrop) och
        # samma larm går inte igen inom timmen.
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(len(mail.outbox), 1)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIn("EUR", campaign.google_error)

    @override_settings(**CONFIGURED)
    def test_after_the_pause_google_is_tried_again(self):
        fake = self.google(TOKEN_OK, customer_row(currency="EUR"))
        self.submit()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            google_attempted_at=timezone.now() - google_publish.RETRY_AFTER - timedelta(minutes=1)
        )
        self.reopen()
        ops = google_publish.build_operations(self.campaign, CID)
        fake.responses = [customer_row(auto_tagging=True), created(ops)]
        self.submit()
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)

    @override_settings(**CONFIGURED)
    def test_the_daily_cap_stops_calls_to_google(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            publish_day=timezone.localdate(timezone.now(), google_publish.limits.STOCKHOLM),
            publish_count=google_publish.limits.PUBLISH_DAILY_MAX,
        )
        fake = self.google()
        self.submit()
        self.assertEqual(fake.requests, [])
        campaign = self.reload()
        self.assertEqual(campaign.google_error, google_publish.MSG_DAILY_LIMIT)
        self.assertIn("dagens gräns", self.queue_html().split('id="att-publicera"')[1])

    @override_settings(**CONFIGURED)
    def test_not_linked_clears_an_old_google_error(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(
            google_error="Kundens Google Ads-konto har valutan EUR, inte SEK."
        )
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        self.google()
        self.submit()
        self.assertEqual(self.reload().google_error, "")
        to_publish = self.queue_html().split('id="att-publicera"')[1]
        self.assertIn("Kontot är inte kopplat under ADX.", to_publish)
        self.assertNotIn("EUR", to_publish)

    @override_settings(**CONFIGURED)
    def test_staff_can_mark_it_live_by_hand_with_the_api_on(self):
        policy = google_error_policy()
        fake = self.google(TOKEN_OK, customer_row(auto_tagging=True), policy)
        self.submit()
        campaign = self.reload()
        self.assertIn("Policy", campaign.google_error)
        page = self.review_page()
        self.assertContains(page, '<details class="mf-more mf-gap" open>')
        self.assertContains(page, 'name="manual" value="1"')
        response = self.post(manual="1", google_campaign_id="4242")
        self.assertEqual(response.status_code, 302)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertEqual(campaign.google_campaign_id, "4242")
        self.assertEqual(campaign.google_error, "")
        self.assertEqual(len(fake.requests), 3)

    @override_settings(**CONFIGURED)
    def test_staff_can_take_it_back_to_review(self):
        self.google(TOKEN_OK, customer_row(auto_tagging=True), google_error_policy())
        self.submit()
        outbox = len(mail.outbox)
        response = self.post(action="return")
        self.assertIn("Kunden har inte mejlats", messages_of(response))
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_IN_REVIEW)
        self.assertIsNone(campaign.approved_at)
        pending = campaign.pending_review()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.submitted_by, self.staff)
        self.assertEqual(len(mail.outbox), outbox)
        # Den ligger att granska, och granskningsformuläret finns.
        self.assertEqual(
            [item.campaign.pk for item in manage_review.to_review_items()], [campaign.pk]
        )
        self.assertContains(self.review_page(), "Klar, skicka till kunden")
        # Kunden ser att ADX tog tillbaka den, inte att kunden skickade den.
        page = self.detail("granskning")
        self.assertContains(page, "ADX tog tillbaka kampanjen för granskning")
        # Bara en godkänd kampanj som inte är publicerad kan tas tillbaka.
        response = self.post(action="return")
        self.assertIn("Inget ändrades", messages_of(response))
        self.assertEqual(campaign.reviews.count(), 1)


def google_error_policy():
    """Googles nej till ett sökord (policy), för hela anropet."""
    from .test_google_ads import google_error

    return google_error(
        400,
        "INVALID_ARGUMENT",
        "policyViolationError",
        "POLICY_ERROR",
        message="Policy",
        location={
            "fieldPathElements": [
                {"fieldName": "mutate_operations", "index": 7},
                {"fieldName": "ad_group_criterion_operation"},
            ]
        },
    )


class NoticeTests(ChoiceFixture, TestCase):
    def test_an_approved_campaign_says_once_that_it_waits_for_the_link(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.STATUS_NEEDS_CUSTOMER, approved_at=timezone.now(), approved_by=self.anna
        )
        html = self.detail().content.decode()
        self.assertEqual(html.count("när ditt Google Ads-konto är kopplat under ADX"), 1)
        self.assertNotIn("Kampanjen kan gå live först när", html)
        self.assertIn(reverse("flamingo:app_google"), html)

    def test_the_status_label_keeps_its_capitals(self):
        html = self.staff_client().get(reverse("manage:flamingo_overview")).content.decode()
        self.assertIn("Google: Kopplat under ADX.", html)
        self.assertNotIn("kopplat under adx", html)
