"""
Publiceringen med Google Ads API (google_publish.py) och vägen för hand som
finns kvar utan API:t: anropets form, live först när Google svarat, fel som
lämnar kampanjen orörd, ett dubbelklick som ger en kampanj, en kampanj som
tas över efter ett försök utan svar, valutan, demokontot och att
betalningen hos Google inte stoppar något.

Inget här når nätet: urlopen byts mot FakeGoogle (test_google_ads.py), som
svarar med det testet köat och fäller testet vid ett oväntat anrop.
"""

import json
from datetime import timedelta
from unittest import mock
from urllib.error import URLError

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.db import OperationalError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.projects.models import Customer

from . import checks, google_ads, google_publish, manage_review, rules
from .models import Campaign, Fact, FlamingoAccount, Review, Service
from .test_google_ads import ACCESS, API, CONFIGURED, NOTHING, TOKEN_OK, FakeGoogle, google_error
from .testing import pages_from_campaigns

User = get_user_model()

CLIENT_ID = "111-222-3333"
CID = "1112223333"
MCC = "1234567890"
CAMPAIGN_RN = f"customers/{CID}/campaigns/555"

HEADLINES = ["Rörjour i Nacka", "Ring rörmokaren", "Akut vattenläcka"]
DESCRIPTIONS = [
    "Vattenläcka eller stopp? Ring oss så kommer en rörmokare.",
    "Rörjour i Nacka och Värmdö.",
]


def customer_row(currency="SEK", auto_tagging=False):
    """Googles svar på frågan om kontots valuta och taggning."""
    row = {"resourceName": f"customers/{CID}", "id": CID, "currencyCode": currency}
    if auto_tagging is not None:
        row["autoTaggingEnabled"] = auto_tagging
    return (200, {"results": [{"customer": row}], "fieldMask": "customer.id"})


def created(operations, campaign_id="555"):
    """Ett lyckat mutate-svar med ett resursnamn per ändring, i ordning."""
    keys = {
        "campaignBudgetOperation": ("campaignBudgetResult", "campaignBudgets"),
        "campaignOperation": ("campaignResult", "campaigns"),
        "campaignCriterionOperation": ("campaignCriterionResult", "campaignCriteria"),
        "adGroupOperation": ("adGroupResult", "adGroups"),
        "adGroupCriterionOperation": ("adGroupCriterionResult", "adGroupCriteria"),
        "adGroupAdOperation": ("adGroupAdResult", "adGroupAds"),
    }
    responses = []
    for n, operation in enumerate(operations, start=1):
        kind = next(iter(operation))
        result, collection = keys[kind]
        rid = campaign_id if kind == "campaignOperation" else str(900 + n)
        responses.append({result: {"resourceName": f"customers/{CID}/{collection}/{rid}"}})
    return (200, {"mutateOperationResponses": responses})


def no_campaign():
    """Sökningen efter kampanjens namn hittar inget."""
    return (200, {"results": []})


def existing_campaign(status="ENABLED", name="Flamingo: Rörjour Nacka"):
    return (
        200,
        {
            "results": [
                {
                    "campaign": {
                        "resourceName": CAMPAIGN_RN,
                        "id": "555",
                        "name": name,
                        "status": status,
                        "campaignBudget": f"customers/{CID}/campaignBudgets/77",
                    }
                }
            ]
        },
    )


class PublishFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            "elin", password="x12345678", is_staff=True, first_name="Elin"
        )
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.customer = Customer.objects.create(name="Lindqvist Rör AB")
        cls.customer.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(
            customer=cls.customer,
            is_enabled=True,
            google_status=FlamingoAccount.GOOGLE_LINKED,
            google_ads_customer_id=CLIENT_ID,
        )
        Fact.objects.create(
            account=cls.account,
            key="telefon",
            label="Telefon",
            value="08-000 00 00",
            source=Fact.SOURCE_SITE,
            confirmed=True,
        )
        cls.service = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL
        )
        cls.campaign = Campaign.objects.create(
            account=cls.account,
            service=cls.service,
            name="Rörjour Nacka",
            status=Campaign.STATUS_NEEDS_CUSTOMER,
            area="Nacka, Värmdö + 15 km",
            radius_km=15,
            daily_budget_kr=200,
            headlines=list(HEADLINES),
            descriptions=list(DESCRIPTIONS),
            keywords=[
                {"text": "rörjour nacka", "match": "phrase"},
                {"text": "+rörmokare nacka", "match": "exact"},
                {"text": "Rörjour Nacka", "match": "phrase"},
            ],
            negatives=["jobb", "utbildning", "Jobb"],
            page={"title": "Rörjour i Nacka", "phone": "08-000 00 00"},
            approved_at=timezone.now() - timedelta(hours=1),
            approved_by=cls.anna,
        )
        Review.objects.create(
            campaign=cls.campaign,
            round=1,
            state=Review.STATE_DONE,
            reviewer=cls.staff,
            reviewed_at=timezone.now() - timedelta(hours=2),
        )
        pages_from_campaigns(cls.campaign)

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def reload(self):
        self.campaign.refresh_from_db()
        return self.campaign

    def staff_client(self):
        client = Client()
        client.force_login(self.staff)
        return client

    def post(self, action="publish", **data):
        return self.staff_client().post(
            reverse("manage:flamingo_publish", args=[self.campaign.pk]),
            {"action": action, **data},
        )

    def review_page(self):
        return self.staff_client().get(reverse("manage:flamingo_review", args=[self.campaign.pk]))

    def operations(self):
        return google_publish.build_operations(self.reload(), CID)

    def google(self, *responses):
        fake = FakeGoogle(*responses)
        patcher = mock.patch("apps.flamingo.google_ads.urlopen", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def make_live(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.STATUS_LIVE,
            published_at=timezone.now(),
            google_campaign_id="555",
            google_resources={"campaign": CAMPAIGN_RN},
        )
        return self.reload()


def messages_of(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


# ---------------------------------------------------------------------------
# Anropets form
# ---------------------------------------------------------------------------


class BuildOperationsTests(PublishFixture, TestCase):
    def test_the_fixture_passes_the_checks(self):
        self.assertEqual(checks.validate(self.campaign), [])

    def test_budget_and_campaign(self):
        ops = self.operations()
        budget = ops[0]["campaignBudgetOperation"]["create"]
        name = f"Flamingo: Rörjour Nacka #{self.campaign.pk}"
        self.assertEqual(
            budget,
            {
                "resourceName": f"customers/{CID}/campaignBudgets/-1",
                "name": name,
                "amountMicros": "200000000",
                "deliveryMethod": "STANDARD",
                "explicitlyShared": False,
            },
        )
        self.assertIsInstance(budget["amountMicros"], str)
        campaign = ops[1]["campaignOperation"]["create"]
        self.assertEqual(
            campaign,
            {
                "resourceName": f"customers/{CID}/campaigns/-2",
                "name": name,
                "advertisingChannelType": "SEARCH",
                "status": "ENABLED",
                "campaignBudget": f"customers/{CID}/campaignBudgets/-1",
                "targetSpend": {},
                "networkSettings": {
                    "targetGoogleSearch": True,
                    "targetSearchNetwork": False,
                    "targetContentNetwork": False,
                    "targetPartnerSearchNetwork": False,
                },
                "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
                "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
            },
        )

    def test_language_places_and_negatives_on_the_campaign(self):
        campaign = self.reload()
        campaign.negatives = ["jobb", {"text": "utbildning", "match": "phrase"}, "Jobb"]
        ops = google_publish.build_operations(campaign, CID)
        temp_campaign = f"customers/{CID}/campaigns/-2"
        criteria = [
            op["campaignCriterionOperation"]["create"]
            for op in ops
            if "campaignCriterionOperation" in op
        ]
        for criterion in criteria:
            self.assertEqual(criterion["campaign"], temp_campaign)
        self.assertEqual(criteria[0]["language"], {"languageConstant": "languageConstants/1015"})
        proximity = [c["proximity"] for c in criteria if "proximity" in c]
        self.assertEqual(
            proximity,
            [
                {
                    "address": {"cityName": "Nacka", "countryCode": "SE"},
                    "radius": 15.0,
                    "radiusUnits": "KILOMETERS",
                },
                {
                    "address": {"cityName": "Värmdö", "countryCode": "SE"},
                    "radius": 15.0,
                    "radiusUnits": "KILOMETERS",
                },
            ],
        )
        negatives = [(c["negative"], c["keyword"]) for c in criteria if "keyword" in c]
        # "Jobb" är samma som "jobb": en gång. Typen följer Editor-filen.
        self.assertEqual(
            negatives,
            [
                (True, {"text": "jobb", "matchType": "BROAD"}),
                (True, {"text": "utbildning", "matchType": "PHRASE"}),
            ],
        )

    def test_ad_group_keywords_and_the_ad(self):
        ops = self.operations()
        kinds = [next(iter(op)) for op in ops]
        group_at = kinds.index("adGroupOperation")
        # Det som pekas på skapas först: budget, kampanj, kriterier, grupp, sökord, annons.
        self.assertEqual(kinds[:2], ["campaignBudgetOperation", "campaignOperation"])
        self.assertTrue(all(k == "campaignCriterionOperation" for k in kinds[2:group_at]))
        self.assertEqual(kinds[-1], "adGroupAdOperation")
        temp_group = f"customers/{CID}/adGroups/-3"
        self.assertEqual(
            ops[group_at]["adGroupOperation"]["create"],
            {
                "resourceName": temp_group,
                "name": "Rörjour",
                "campaign": f"customers/{CID}/campaigns/-2",
                "status": "ENABLED",
                "type": "SEARCH_STANDARD",
            },
        )
        keywords = [op["adGroupCriterionOperation"]["create"] for op in ops[group_at + 1 : -1]]
        self.assertEqual(
            keywords,
            [
                {
                    "adGroup": temp_group,
                    "status": "ENABLED",
                    "keyword": {"text": "rörjour nacka", "matchType": "PHRASE"},
                },
                {
                    "adGroup": temp_group,
                    "status": "ENABLED",
                    "keyword": {"text": "rörmokare nacka", "matchType": "EXACT"},
                },
            ],
        )
        ad = ops[-1]["adGroupAdOperation"]["create"]
        self.assertEqual(ad["adGroup"], temp_group)
        self.assertEqual(ad["status"], "ENABLED")
        self.assertEqual(ad["ad"]["finalUrls"], [f"https://adx.se/lp/{self.campaign.page_slug}/"])
        self.assertEqual(
            ad["ad"]["responsiveSearchAd"],
            {
                "headlines": [{"text": h} for h in HEADLINES],
                "descriptions": [{"text": d} for d in DESCRIPTIONS],
            },
        )

    def test_only_temporary_negative_ids_and_plain_json(self):
        text = json.dumps(self.operations())
        for name in ("campaignBudgets/-1", "campaigns/-2", "adGroups/-3"):
            self.assertIn(f"customers/{CID}/{name}", text)

    @override_settings(FLAMINGO_LANDING_BASE_URL="https://sidor.adx.se/")
    def test_the_final_url_follows_the_landing_domain(self):
        ad = self.operations()[-1]["adGroupAdOperation"]["create"]
        self.assertEqual(
            ad["ad"]["finalUrls"], [f"https://sidor.adx.se/lp/{self.campaign.page_slug}/"]
        )

    def test_an_area_without_places_is_refused(self):
        self.campaign.area = "15 km"
        with self.assertRaises(ValueError):
            google_publish.build_operations(self.campaign, CID)

    def test_a_failing_operation_is_named_for_the_staff(self):
        ops = self.operations()
        labels = [google_publish.describe_operation(op) for op in ops]
        self.assertEqual(labels[:3], ["budgeten", "kampanjen", "språket"])
        self.assertIn("orten Nacka", labels)
        self.assertIn('det negativa sökordet "jobb"', labels)
        self.assertIn('sökordet "rörmokare nacka"', labels)
        self.assertEqual(labels[-1], "annonsen")


# ---------------------------------------------------------------------------
# Live hos Google
# ---------------------------------------------------------------------------


@override_settings(**CONFIGURED)
class GoLiveTests(PublishFixture, TestCase):
    def test_success_goes_live_only_after_google_answered(self):
        ops = self.operations()
        fake = self.google(TOKEN_OK, customer_row(), (200, {}), created(ops))
        result = google_publish.go_live(self.reload(), self.staff)
        self.assertEqual(result.campaign_id, "555")
        self.assertFalse(result.adopted)
        self.assertTrue(result.auto_tagging_enabled)

        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertIsNotNone(campaign.published_at)
        self.assertEqual(campaign.google_campaign_id, "555")
        self.assertEqual(campaign.google_resources["campaign"], CAMPAIGN_RN)
        self.assertEqual(
            campaign.google_resources["budget"], f"customers/{CID}/campaignBudgets/901"
        )
        self.assertTrue(campaign.google_resources["ad_group"].startswith(f"customers/{CID}/adG"))
        self.assertTrue(campaign.google_resources["ad"].startswith(f"customers/{CID}/adGroupAds/"))
        self.assertEqual(len(campaign.google_resources["criteria"]), len(ops) - 4)
        self.assertIsNotNone(campaign.google_synced_at)
        self.assertEqual(campaign.google_error, "")
        self.assertIsNotNone(campaign.google_publish_started_at)

        urls = [r.full_url for r in fake.requests]
        base = f"https://googleads.googleapis.com/{API}/customers/{CID}"
        self.assertEqual(
            urls,
            [
                google_ads.TOKEN_URL,
                f"{base}/googleAds:search",
                f"{base}:mutate",
                f"{base}/googleAds:mutate",
            ],
        )
        self.assertIn("customer.currency_code", fake.body(1)["query"])
        self.assertEqual(
            fake.body(2),
            {
                "operation": {
                    "update": {"resourceName": f"customers/{CID}", "autoTaggingEnabled": True},
                    "updateMask": "autoTaggingEnabled",
                }
            },
        )
        self.assertEqual(
            fake.body(3),
            {"mutateOperations": ops, "partialFailure": False, "validateOnly": False},
        )
        for request in fake.requests[1:]:
            self.assertEqual(request.get_header("Login-customer-id"), MCC)
        self.account.refresh_from_db()
        self.assertIs(self.account.google_auto_tagging, True)
        self.assertEqual(mail.outbox, [])

    def test_auto_tagging_already_on_is_left_alone(self):
        ops = self.operations()
        fake = self.google(TOKEN_OK, customer_row(auto_tagging=True), created(ops))
        result = google_publish.go_live(self.reload(), self.staff)
        self.assertFalse(result.auto_tagging_enabled)
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)

    def test_the_view_publishes_and_opens_the_landing_page(self):
        self.assertEqual(Client().get(self.campaign.landing_url).status_code, 404)
        ops = self.operations()
        self.google(TOKEN_OK, customer_row(auto_tagging=True), created(ops))
        response = self.post()
        self.assertRedirects(
            response,
            reverse("manage:flamingo_review", args=[self.campaign.pk]) + "#publicering",
            fetch_redirect_response=False,
        )
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)
        text = " ".join(messages_of(response))
        self.assertIn("är live hos Google (kampanj 555)", text)
        self.assertIn("Annonserna visas först när kunden lagt in betalning", text)
        self.assertIn("Kunden har inte mejlats", text)
        self.assertEqual(Client().get(self.campaign.landing_url).status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_a_google_error_leaves_the_campaign_unchanged(self):
        ops = self.operations()
        keyword_at = next(i for i, op in enumerate(ops) if "adGroupCriterionOperation" in op)
        refusal = google_error(
            400,
            "INVALID_ARGUMENT",
            "policyFindingError",
            "POLICY_FINDING",
            message=f"Ett policyfel med {ACCESS} i texten.",
            location={
                "fieldPathElements": [{"fieldName": "mutate_operations", "index": keyword_at}]
            },
        )
        self.google(TOKEN_OK, customer_row(auto_tagging=True), refusal)
        response = self.post()
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIsNone(campaign.published_at)
        self.assertEqual(campaign.google_campaign_id, "")
        self.assertEqual(campaign.google_resources, {})
        self.assertIn('gäller sökordet "rörjour nacka"', campaign.google_error)
        self.assertNotIn(ACCESS, campaign.google_error)
        # Google sa nej till hela anropet: inget skapades, spärren är släppt.
        self.assertIsNone(campaign.google_publish_started_at)
        text = " ".join(messages_of(response))
        self.assertIn("Inget publicerades.", text)
        self.assertNotIn(ACCESS, text)
        self.assertEqual(Client().get(self.campaign.landing_url).status_code, 404)

        page = self.review_page()
        self.assertContains(page, "Senaste felet vid publiceringen")
        self.assertNotContains(page, ACCESS)

    def test_double_submit_creates_one_campaign(self):
        ops = self.operations()
        fake = self.google(TOKEN_OK, customer_row(auto_tagging=True), created(ops))
        self.post()
        response = self.post()
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)
        self.assertIn("Kampanjen är redan live.", messages_of(response))
        creates = [r for r in fake.requests if r.full_url.endswith("googleAds:mutate")]
        self.assertEqual(len(creates), 1)
        # Även direkt: redan live, inget anrop.
        result = google_publish.go_live(self.reload(), self.staff)
        self.assertTrue(result.already_live)
        self.assertEqual(len(fake.requests), 3)

    def test_a_click_that_cannot_get_the_lock_calls_nobody(self):
        fake = self.google()
        with mock.patch.object(
            Campaign.objects, "select_for_update", side_effect=OperationalError("låst")
        ):
            with self.assertRaises(google_publish.PublishError) as caught:
                google_publish.go_live(self.reload(), self.staff)
        self.assertEqual(caught.exception.message, google_publish.MSG_BUSY)
        self.assertEqual(fake.requests, [])
        self.assertEqual(self.reload().status, Campaign.STATUS_NEEDS_CUSTOMER)

    def test_no_answer_keeps_the_claim_and_the_retry_adopts_the_campaign(self):
        fake = self.google(
            TOKEN_OK,
            customer_row(auto_tagging=True),
            (URLError("timeout"), None),
        )
        with self.assertRaises(google_publish.PublishError):
            google_publish.go_live(self.reload(), self.staff)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        # Byrån får veta att kampanjen kan vara igång hos Google medan
        # landningssidan är stängd.
        self.assertTrue(campaign.google_error.startswith(google_ads.MSG_UNREACHABLE))
        self.assertIn("igång hos Google medan landningssidan är stängd", campaign.google_error)
        # Svaret uteblev: kampanjen kan finnas hos Google, så spärren ligger kvar.
        self.assertIsNotNone(campaign.google_publish_started_at)
        # Det försöket skickade sparades med spärren.
        self.assertEqual(
            campaign.google_publish_sent,
            google_publish.sent_marker(campaign, CID),
        )

        fake.responses = [existing_campaign(), customer_row(auto_tagging=True)]
        result = google_publish.go_live(self.reload(), self.staff)
        self.assertTrue(result.adopted)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertEqual(campaign.google_campaign_id, "555")
        self.assertEqual(campaign.google_resources["campaign"], CAMPAIGN_RN)
        self.assertTrue(campaign.google_resources["adopted"])
        self.assertEqual(campaign.google_error, "")
        query = fake.body(3)["query"]
        self.assertIn(f"campaign.name IN ('Flamingo: Rörjour Nacka #{campaign.pk}')", query)
        self.assertIn("campaign.status != 'REMOVED'", query)
        creates = [r for r in fake.requests if r.full_url.endswith("googleAds:mutate")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(fake.requests), 5)

    def test_an_adopted_campaign_that_is_paused_is_turned_on(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(
            google_publish_started_at=timezone.now() - timedelta(minutes=30)
        )
        fake = self.google(
            TOKEN_OK,
            existing_campaign("PAUSED"),
            customer_row(auto_tagging=True),
            (
                200,
                {"mutateOperationResponses": [{"campaignResult": {"resourceName": CAMPAIGN_RN}}]},
            ),
        )
        result = google_publish.go_live(self.reload(), self.staff)
        self.assertTrue(result.adopted)
        self.assertEqual(
            fake.body(3)["mutateOperations"],
            [
                {
                    "campaignOperation": {
                        "update": {"resourceName": CAMPAIGN_RN, "status": "ENABLED"},
                        "updateMask": "status",
                    }
                }
            ],
        )
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)

    def test_a_claim_with_nothing_at_google_creates_once(self):
        """Spärren satt (ett klick till, eller ett försök som dog innan
        anropet), men sökningen hittar inget: då skapas kampanjen en gång."""
        Campaign.objects.filter(pk=self.campaign.pk).update(
            google_publish_started_at=timezone.now()
        )
        ops = self.operations()
        fake = self.google(TOKEN_OK, no_campaign(), customer_row(auto_tagging=True), created(ops))
        result = google_publish.go_live(self.reload(), self.staff)
        self.assertFalse(result.adopted)
        self.assertEqual(fake.body(3)["mutateOperations"], ops)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        # Spärren togs om, med det som skickades.
        self.assertEqual(campaign.google_publish_sent, google_publish.sent_marker(campaign, CID))

    def first_attempt_times_out(self):
        """Ett försök med budgeten 200 kr som inte fick svar."""
        fake = self.google(TOKEN_OK, customer_row(auto_tagging=True), (URLError("timeout"), None))
        with self.assertRaises(google_publish.PublishError):
            google_publish.go_live(self.reload(), self.staff)
        self.assertEqual(
            fake.body(2)["mutateOperations"][0]["campaignBudgetOperation"]["create"][
                "amountMicros"
            ],
            "200000000",
        )
        return fake

    def test_a_changed_campaign_is_not_adopted_and_the_old_one_is_paused(self):
        fake = self.first_attempt_times_out()
        # Kunden ändrade budgeten och skickade igen (ett nytt godkännande).
        Campaign.objects.filter(pk=self.campaign.pk).update(daily_budget_kr=60)
        fake.responses = [
            existing_campaign(name=f"Flamingo: Rörjour Nacka #{self.campaign.pk}"),
            (
                200,
                {"mutateOperationResponses": [{"campaignResult": {"resourceName": CAMPAIGN_RN}}]},
            ),
        ]
        with self.assertRaises(google_publish.PublishError) as caught:
            google_publish.go_live(self.reload(), self.staff)
        self.assertIn("har ändrats sedan dess", caught.exception.message)
        self.assertIn("är pausad hos Google", caught.exception.message)
        self.assertIn("Ta bort den gamla kampanjen", caught.exception.message)
        # Den gamla kampanjen pausades hos Google; inget nytt skapades.
        self.assertEqual(
            fake.body(4)["mutateOperations"],
            [
                {
                    "campaignOperation": {
                        "update": {"resourceName": CAMPAIGN_RN, "status": "PAUSED"},
                        "updateMask": "status",
                    }
                }
            ],
        )
        creates = [r for r in fake.requests if r.full_url.endswith("googleAds:mutate")]
        self.assertEqual(len(creates), 2)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIn("Ta bort den gamla kampanjen", campaign.google_error)
        # Spärren ligger kvar: den gamla finns hos Google tills byrån tagit bort den.
        self.assertIsNotNone(campaign.google_publish_started_at)

    def test_a_renamed_campaign_is_looked_up_by_both_names(self):
        fake = self.first_attempt_times_out()
        Campaign.objects.filter(pk=self.campaign.pk).update(name="Jour Nacka")
        fake.responses = [
            existing_campaign(name=f"Flamingo: Rörjour Nacka #{self.campaign.pk}"),
            (
                200,
                {"mutateOperationResponses": [{"campaignResult": {"resourceName": CAMPAIGN_RN}}]},
            ),
        ]
        with self.assertRaises(google_publish.PublishError):
            google_publish.go_live(self.reload(), self.staff)
        query = fake.body(3)["query"]
        self.assertIn(f"'Flamingo: Rörjour Nacka #{self.campaign.pk}'", query)
        self.assertIn(f"'Flamingo: Jour Nacka #{self.campaign.pk}'", query)

    def test_after_the_old_one_is_removed_the_new_content_is_created(self):
        fake = self.first_attempt_times_out()
        Campaign.objects.filter(pk=self.campaign.pk).update(daily_budget_kr=60)
        ops = self.operations()
        fake.responses = [no_campaign(), customer_row(auto_tagging=True), created(ops)]
        result = google_publish.go_live(self.reload(), self.staff)
        self.assertFalse(result.adopted)
        sent = fake.body(5)["mutateOperations"]
        self.assertEqual(sent[0]["campaignBudgetOperation"]["create"]["amountMicros"], "60000000")
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)

    def test_an_id_another_account_has_is_never_published(self):
        fake = self.google()
        with mock.patch.object(
            FlamingoAccount, "google_id_shared", new_callable=mock.PropertyMock
        ) as shared:
            shared.return_value = True
            with self.assertRaises(google_publish.PublishError) as caught:
                google_publish.go_live(self.reload(), self.staff)
            self.assertEqual(caught.exception.message, google_publish.MSG_SHARED_ID)
            self.assertEqual(
                google_publish.approval_path(self.account), google_publish.OUTCOME_NOT_LINKED
            )
        self.assertEqual(fake.requests, [])

    def test_an_account_not_in_sek_is_refused(self):
        fake = self.google(TOKEN_OK, customer_row(currency="EUR"))
        response = self.post()
        text = " ".join(messages_of(response))
        self.assertIn("valutan EUR, inte SEK", text)
        self.assertIn("Inget publicerades", text)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIn("valutan EUR", campaign.google_error)
        self.assertIsNone(campaign.google_publish_started_at)
        self.assertEqual(len(fake.requests), 2)

    def test_problems_from_the_checks_stop_it_before_google(self):
        fake = self.google()
        problem = checks.Problem("headlines", "Siffran 24 finns inte bland uppgifterna.", 0)
        with mock.patch.object(checks, "validate", return_value=[problem]):
            response = self.post()
        self.assertIn("Kontrollerna hittade 1 problem", " ".join(messages_of(response)))
        self.assertEqual(fake.requests, [])
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertIsNone(campaign.google_publish_started_at)

    def test_a_new_round_or_missing_approval_never_reaches_google(self):
        fake = self.google()
        Review.objects.create(campaign=self.campaign, round=2)
        self.post()
        self.assertEqual(self.reload().status, Campaign.STATUS_NEEDS_CUSTOMER)
        Review.objects.filter(campaign=self.campaign, round=2).delete()
        Campaign.objects.filter(pk=self.campaign.pk).update(approved_at=None)
        self.post()
        self.assertEqual(self.reload().status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertEqual(fake.requests, [])

    def test_the_demo_account_never_calls_google(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        fake = self.google()
        with self.assertRaises(google_publish.PublishError) as caught:
            google_publish.go_live(self.reload(), self.staff)
        self.assertEqual(caught.exception.message, google_publish.MSG_DEMO)
        # I vyn tar demokontot vägen för hand, utan Google.
        response = self.post()
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)
        self.assertIn("Kunden har inte mejlats", " ".join(messages_of(response)))
        self.make_live()
        self.post("pause")
        self.assertEqual(self.reload().status, Campaign.STATUS_PAUSED)
        self.assertEqual(fake.requests, [])


# ---------------------------------------------------------------------------
# Pausa och återuppta
# ---------------------------------------------------------------------------


@override_settings(**CONFIGURED)
class PauseResumeTests(PublishFixture, TestCase):
    def test_pause_and_resume_at_google_first(self):
        self.make_live()
        ok = (
            200,
            {"mutateOperationResponses": [{"campaignResult": {"resourceName": CAMPAIGN_RN}}]},
        )
        fake = self.google(TOKEN_OK, ok, ok)
        response = self.post("pause")
        self.assertEqual(self.reload().status, Campaign.STATUS_PAUSED)
        self.assertIn("Pausad hos Google och här", " ".join(messages_of(response)))
        self.assertEqual(
            fake.body(1)["mutateOperations"],
            [
                {
                    "campaignOperation": {
                        "update": {"resourceName": CAMPAIGN_RN, "status": "PAUSED"},
                        "updateMask": "status",
                    }
                }
            ],
        )
        self.assertTrue(fake.requests[1].full_url.endswith(f"customers/{CID}/googleAds:mutate"))
        self.post("resume")
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)
        self.assertEqual(
            fake.body(2)["mutateOperations"][0]["campaignOperation"]["update"]["status"],
            "ENABLED",
        )
        self.assertEqual(mail.outbox, [])

    def test_a_google_error_on_pause_changes_nothing_here(self):
        self.make_live()
        self.google(
            TOKEN_OK,
            google_error(403, "PERMISSION_DENIED", "authorizationError", "USER_PERMISSION_DENIED"),
        )
        response = self.post("pause")
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertIn("Inloggningen når inte", campaign.google_error)
        text = " ".join(messages_of(response))
        self.assertIn("Inget pausades.", text)
        self.assertIn("Pausa kampanjen i Google Ads för hand", text)

    def test_a_campaign_published_by_hand_is_paused_by_hand(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.STATUS_LIVE, published_at=timezone.now(), google_campaign_id="42"
        )
        fake = self.google()
        response = self.post("pause")
        self.assertEqual(self.reload().status, Campaign.STATUS_PAUSED)
        self.assertIn("Pausa kampanjen i Google Ads också", " ".join(messages_of(response)))
        self.assertEqual(fake.requests, [])
        page = self.review_page()
        self.assertContains(page, google_publish.MSG_MANUAL_PUBLISHED)

    def test_without_the_api_a_google_campaign_is_paused_by_hand(self):
        self.make_live()
        fake = self.google()
        with override_settings(**NOTHING):
            self.post("pause")
        self.assertEqual(self.reload().status, Campaign.STATUS_PAUSED)
        self.assertEqual(fake.requests, [])


# ---------------------------------------------------------------------------
# Vägen för hand, panelen och betalningen
# ---------------------------------------------------------------------------


@override_settings(**NOTHING)
class PanelAndBlockerTests(PublishFixture, TestCase):
    def test_manual_path_when_the_api_is_not_configured(self):
        fake = self.google()
        with override_settings(**NOTHING):
            page = self.review_page()
            self.assertContains(page, "Google Ads API är inte inkopplat")
            self.assertContains(page, "Markera som live</button>")
            self.assertNotContains(page, "Publicera hos Google")
            self.post(google_campaign_id="12 345")
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertEqual(campaign.google_campaign_id, "12345")
        self.assertEqual(campaign.google_resources, {})
        self.assertEqual(fake.requests, [])

    def test_only_a_developer_token_is_still_the_manual_path(self):
        with override_settings(**{**NOTHING, "GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token"}):
            page = self.review_page()
        self.assertContains(page, "Google Ads API är inte inkopplat")
        self.assertContains(page, "Markera som live</button>")

    @override_settings(**CONFIGURED)
    def test_the_api_panel(self):
        page = self.review_page()
        self.assertContains(page, "Google Ads API.")
        self.assertContains(page, "Publicera hos Google</button>")
        self.assertContains(page, "Nacka, Värmdö, radie 15 km")
        self.assertContains(page, "2 sökord och 2 negativa sökord")
        # Vägen för hand finns ändå, hopfälld tills Google sagt nej.
        self.assertContains(page, '<details class="mf-more mf-gap">')
        self.assertContains(page, "Publicera för hand i stället")
        self.assertContains(page, "Tillbaka till granskning</button>")
        self.assertContains(page, google_publish.MSG_BILLING)
        self.make_live()
        page = self.review_page()
        self.assertContains(page, "Kampanj 555 i konto 111-222-3333")
        self.assertContains(page, "https://ads.google.com/aw/campaigns?campaignId=555")
        self.assertContains(page, "Pausa hos Google</button>")

    def test_billing_never_blocks_but_linking_does(self):
        self.assertEqual(manage_review._publish_blockers(self.campaign, None), [])
        page = self.review_page()
        self.assertContains(page, google_publish.MSG_BILLING)
        self.assertContains(page, "Markera som live</button>")
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_BILLING_OK
        )
        self.assertNotContains(self.review_page(), google_publish.MSG_BILLING)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        self.campaign.account.refresh_from_db()
        blockers = manage_review._publish_blockers(self.reload(), None)
        self.assertEqual(len(blockers), 1)
        self.assertIn("inte markerat som kopplat under ADX", blockers[0])
        self.post()
        self.assertEqual(self.reload().status, Campaign.STATUS_NEEDS_CUSTOMER)

    def test_a_linked_account_needs_its_id(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(google_ads_customer_id="")
        blockers = manage_review._publish_blockers(self.reload(), None)
        self.assertEqual(blockers, ["Google Ads-kontots id saknas. Skriv det på kundkortet."])
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.assertEqual(manage_review._publish_blockers(self.reload(), None), [])

    def test_publish_by_hand_without_billing(self):
        with override_settings(**NOTHING):
            self.post()
        self.assertEqual(self.reload().status, Campaign.STATUS_LIVE)

    def test_billing_status_from_google_counts_as_done(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(google_billing_status="APPROVED")
        self.account.refresh_from_db()
        self.assertFalse(google_publish.billing_missing(self.account))
        self.assertNotIn(
            "google_billing", [t.key for t in rules.onboarding_things(self.account, None)]
        )

    def test_the_google_step_is_done_when_linked(self):
        google = rules.onboarding_for(self.account).steps[2]
        self.assertEqual(google.state, "done")
        keys = [t.key for t in rules.onboarding_things(self.account, None)]
        self.assertIn("google_billing", keys)
        thing = next(
            t for t in rules.onboarding_things(self.account, None) if t.key == "google_billing"
        )
        self.assertNotIn("kan gå live", thing.text)
        self.assertIn("Annonserna visas först", thing.text)
