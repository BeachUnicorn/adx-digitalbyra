"""ADX Flamingo, steg 2 (README, Giovanni 2026-10-28): Google-annonserna
leder till sidans egen adress med kampanjens nummer.

    KampanjParamTests     ?kampanj=<id> på /lp/<konto>/<sida>/: besöket, klicket
                          på numret och förfrågan blir kampanjens, med gclid en
                          konvertering; ett främmande, okänt, felaktigt eller
                          annan sidas id ignoreras; ut vinner som förut; ett
                          gclid utan kampanj går inte till Google
    PublishPayloadTests   anropet till Google: annonsens slutadress är sidans
                          egen och kampanjens suffix kampanj=<id>; adressen
                          sparas när kampanjen blivit live, också för hand;
                          paus och återupptagning skriver inte om annonserna
    CampaignTabTests      fliken Sidan säger vart annonserna leder (sidans egen
                          adress, kampanjens för en äldre kampanj, den förra
                          sidan efter ett byte) och byrån får veta om bytet
"""

from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.utskick import tokens

from . import exports, google_publish, leads, pagebuilder, sources
from .models import Campaign, ConversionUpload, LandingPage, Lead, PageVisitDay
from .test_google_ads import CONFIGURED, NOTHING, TOKEN_OK
from .test_google_publish import CID, PublishFixture, created, customer_row
from .test_lp_sources import FORM, LINK_SETTINGS, SourcesFixture, visitor, visits


def kampanj(url, campaign_id, **extra):
    query = "&".join([f"kampanj={campaign_id}", *(f"{k}={v}" for k, v in extra.items())])
    return f"{url}?{query}"


# ---------------------------------------------------------------------------
# ?kampanj= på sidans egen adress
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class KampanjParamTests(SourcesFixture, TestCase):
    def call_url(self, page=None):
        page = page or self.page
        return reverse("flamingo_public:page_call_click", args=[page.account.lp_slug, page.slug])

    def test_the_visit_counts_as_an_ad_for_that_campaign(self):
        response = visitor().get(kampanj(self.own_url(), self.campaign.pk, gclid="Cj0ad"))
        self.assertEqual(response.status_code, 200)
        # Sidan ritas som på sidans egen adress (ingen kampanj i mallen).
        self.assertIsNone(response.context["campaign"])
        self.assertEqual(visits(self.page, f"ad:c{self.campaign.pk}"), 1)
        row = PageVisitDay.objects.get(page=self.page)
        self.assertEqual((row.source, row.campaign_id), ("ad", self.campaign.pk))

    def test_the_lead_gets_the_campaign_and_goes_to_google_with_a_gclid(self):
        url = kampanj(self.own_url(), self.campaign.pk, gclid="Cj0ad")
        response = visitor().post(url, {**FORM, "gclid": "Cj0ad"})
        self.assertEqual(response.status_code, 302)
        lead = Lead.objects.get()
        self.assertEqual(lead.campaign_id, self.campaign.pk)
        self.assertEqual(lead.service_id, self.service.pk)
        self.assertEqual((lead.landing_page_id, lead.page_source), (self.page.pk, "ad"))
        self.assertEqual(lead.gclid, "Cj0ad")
        # Kampanjens nummer är ingen utm.
        self.assertNotIn("kampanj", lead.utm)
        self.assertTrue(lead.can_send_to_google)
        upload = ConversionUpload.objects.get(lead=lead)
        self.assertEqual(upload.kind, ConversionUpload.KIND_LEAD)

    def test_the_form_action_keeps_the_campaign(self):
        url = kampanj(self.own_url(), self.campaign.pk, gclid="Cj0ad")
        html = visitor().get(url).content.decode()
        self.assertIn(f'action="{self.own_url()}?kampanj={self.campaign.pk}&amp;gclid=Cj0ad"', html)

    def test_a_won_deal_from_the_own_address_goes_to_google(self):
        url = kampanj(self.own_url(), self.campaign.pk)
        visitor().post(url, {**FORM, "gclid": "Cj0deal"})
        lead = Lead.objects.get()
        leads.set_status(lead, Lead.STATUS_WON, "12 000")
        kinds = set(ConversionUpload.objects.filter(lead=lead).values_list("kind", flat=True))
        self.assertEqual(kinds, {ConversionUpload.KIND_LEAD, ConversionUpload.KIND_DEAL})

    def test_the_call_click_gets_the_campaign_and_its_limits(self):
        data = {"kampanj": str(self.campaign.pk), "gclid": "Cj0call"}
        self.assertEqual(visitor().post(self.call_url(), data).status_code, 204)
        lead = Lead.objects.get(source=Lead.SOURCE_CALL_CLICK)
        self.assertEqual(lead.campaign_id, self.campaign.pk)
        self.assertEqual((lead.landing_page_id, lead.page_source), (self.page.pk, "ad"))
        self.assertTrue(lead.can_send_to_google)
        self.assertEqual(ConversionUpload.objects.get(lead=lead).kind, ConversionUpload.KIND_CALL)
        # Kampanjens spärrar: samma besökare och klick räknas en gång i timmen.
        visitor().post(self.call_url(), data)
        self.assertEqual(Lead.objects.filter(source=Lead.SOURCE_CALL_CLICK).count(), 1)

    def test_the_script_sends_the_campaign_with_the_call_click(self):
        script = (settings.BASE_DIR / "static/js/flamingo-lp.js").read_text()
        self.assertIn('"kampanj",', script)

    def test_another_accounts_campaign_is_ignored(self):
        other = Campaign.objects.create(
            account=self.other_account,
            service=self.other_page.account.services.create(name="Spolning annan"),
            name="Annans kampanj",
            status=Campaign.STATUS_LIVE,
            landing_page=self.other_page,
        )
        visitor().get(kampanj(self.own_url(), other.pk, gclid="Cj0x"))
        self.assertEqual(visits(self.page, "ad"), 1)
        self.assertFalse(PageVisitDay.objects.filter(campaign=other).exists())
        visitor().post(kampanj(self.own_url(), other.pk), {**FORM, "gclid": "Cj0x"})
        lead = Lead.objects.get()
        self.assertIsNone(lead.campaign_id)
        self.assertEqual(lead.page_source, "ad")
        self.assertFalse(lead.can_send_to_google)
        self.assertFalse(ConversionUpload.objects.exists())
        visitor(ip="203.0.113.77").post(self.call_url(), {"kampanj": str(other.pk)})
        click = Lead.objects.get(source=Lead.SOURCE_CALL_CLICK)
        self.assertIsNone(click.campaign_id)

    def test_a_campaign_leading_to_another_page_is_ignored(self):
        second = LandingPage.objects.create(
            account=self.account,
            name="Stopp i avloppet",
            draft={"blocks": self.page.draft_blocks},
            published={"blocks": self.page.published_blocks},
            published_at=timezone.now(),
        )
        mismatch = Campaign.objects.create(
            account=self.account,
            service=self.service,
            name="Stopp Nacka",
            status=Campaign.STATUS_LIVE,
            landing_page=second,
        )
        visitor().get(kampanj(self.own_url(), mismatch.pk))
        self.assertEqual(visits(self.page, "direct"), 1)
        visitor().post(kampanj(self.own_url(), mismatch.pk), {**FORM, "gclid": "Cj0m"})
        lead = Lead.objects.get()
        self.assertIsNone(lead.campaign_id)
        self.assertFalse(lead.can_send_to_google)
        # På sin egen sida räknas den.
        visitor().get(kampanj(second.landing_url, mismatch.pk))
        self.assertEqual(visits(second, f"ad:c{mismatch.pk}"), 1)

    def test_the_page_the_ads_were_published_with_still_counts(self):
        """En kampanj som bytt sida efter publiceringen: annonserna landar
        kvar på den förra sidan (Campaign.ads_final_url)."""
        second = LandingPage.objects.create(account=self.account, name="Ny sida")
        Campaign.objects.filter(pk=self.campaign.pk).update(
            landing_page=second, ads_final_url=exports.landing_page_url(self.page)
        )
        visitor().get(kampanj(self.own_url(), self.campaign.pk))
        self.assertEqual(visits(self.page, f"ad:c{self.campaign.pk}"), 1)
        # Utan sparad adress leder kampanjen inte hit längre.
        Campaign.objects.filter(pk=self.campaign.pk).update(ads_final_url="")
        visitor(ip="203.0.113.12").get(kampanj(self.own_url(), self.campaign.pk))
        self.assertEqual(visits(self.page, "direct"), 1)

    def test_unknown_and_malformed_ids_are_ignored(self):
        for n, raw in enumerate(("999999", "0", "-1", "12abc", "1e3", "", "99999999999")):
            with self.subTest(raw=raw):
                ip = f"203.0.113.{100 + n}"
                response = visitor(ip=ip).get(f"{self.own_url()}?kampanj={raw}")
                self.assertEqual(response.status_code, 200)
        self.assertEqual(visits(self.page, "direct"), 7)
        self.assertEqual(leads.campaign_param({"kampanj": "12"}), 12)
        for raw in ("0", "012", "-1", "12abc", "1234567890", ["", "x"]):
            with self.subTest(raw=raw):
                self.assertIsNone(leads.campaign_param({"kampanj": raw}))
        self.assertIsNone(sources.ad_campaign_for(self.page, None))
        self.assertIsNone(sources.ad_campaign_for(None, self.campaign.pk))

    def test_an_utskick_click_wins_and_never_goes_to_google(self):
        utskick = self.utskick()
        click = self.click("sms", utskick=utskick)
        ut = tokens.ut_token(click.pk)
        visitor().get(kampanj(self.own_url(), self.campaign.pk, ut=ut))
        self.assertEqual(visits(self.page, f"sms:u{utskick.pk}"), 1)
        visitor().post(kampanj(self.own_url(), self.campaign.pk), {**FORM, "ut": ut, "gclid": "g"})
        lead = Lead.objects.get()
        self.assertEqual((lead.page_source, lead.utskick_id), ("sms", utskick.pk))
        # Som på kampanjens adress: kampanjen finns på förfrågan, men ett
        # utskick går aldrig till Google.
        self.assertEqual(lead.campaign_id, self.campaign.pk)
        self.assertFalse(lead.can_send_to_google)
        self.assertFalse(ConversionUpload.objects.exists())

    def test_a_gclid_without_a_campaign_is_an_ad_without_a_conversion(self):
        visitor().post(f"{self.own_url()}?gclid=Cj0alone", {**FORM, "gclid": "Cj0alone"})
        lead = Lead.objects.get()
        self.assertEqual((lead.page_source, lead.campaign_id), ("ad", None))
        self.assertFalse(lead.can_send_to_google)
        self.assertFalse(ConversionUpload.objects.exists())

    def test_the_campaign_address_ignores_the_parameter(self):
        other = Campaign.objects.create(
            account=self.account,
            service=self.service,
            name="Annan kampanj",
            status=Campaign.STATUS_LIVE,
            landing_page=self.page,
        )
        visitor().get(kampanj(self.campaign.landing_url, other.pk))
        self.assertEqual(visits(self.page, f"ad:c{self.campaign.pk}"), 1)
        visitor().post(kampanj(self.campaign.landing_url, other.pk), FORM)
        self.assertEqual(Lead.objects.get().campaign_id, self.campaign.pk)

    def test_an_old_address_redirects_with_the_campaign(self):
        old = self.page.slug
        LandingPage.objects.filter(pk=self.page.pk).update(slug="spolning")
        from .models import LandingPageOldSlug

        LandingPageOldSlug.objects.create(account=self.account, page=self.page, slug=old)
        response = visitor().get(f"/lp/exempelror/{old}/?kampanj={self.campaign.pk}&gclid=g1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], f"/lp/exempelror/spolning/?kampanj={self.campaign.pk}&gclid=g1"
        )


# ---------------------------------------------------------------------------
# Anropet till Google
# ---------------------------------------------------------------------------


@override_settings(**CONFIGURED)
class PublishPayloadTests(PublishFixture, TestCase):
    def own(self):
        page = self.reload().landing_page
        return f"https://adx.se/lp/{page.account.lp_slug}/{page.slug}/"

    def test_the_ad_leads_to_the_own_address_with_the_campaign_suffix(self):
        ops = self.operations()
        campaign_op = ops[1]["campaignOperation"]["create"]
        self.assertEqual(campaign_op["finalUrlSuffix"], f"kampanj={self.campaign.pk}")
        ad = ops[-1]["adGroupAdOperation"]["create"]["ad"]
        self.assertEqual(ad["finalUrls"], [self.own()])
        self.assertNotIn(self.campaign.page_slug, ad["finalUrls"][0])

    def test_the_sent_payload_and_the_stored_address(self):
        ops = self.operations()
        fake = self.google(TOKEN_OK, customer_row(), (200, {}), created(ops))
        google_publish.go_live(self.reload(), self.staff)
        body = fake.body(3)["mutateOperations"]
        self.assertEqual(
            body[1]["campaignOperation"]["create"]["finalUrlSuffix"], f"kampanj={self.campaign.pk}"
        )
        self.assertEqual(body[-1]["adGroupAdOperation"]["create"]["ad"]["finalUrls"], [self.own()])
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertEqual(campaign.ads_final_url, self.own())
        # Sidan publicerades med kampanjen, så den egna adressen är öppen.
        self.assertTrue(campaign.landing_page.is_published)
        self.assertEqual(exports.ads_url(campaign), f"{self.own()}?kampanj={campaign.pk}")

    def test_pause_and_resume_never_rewrite_the_ads(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.STATUS_LIVE,
            published_at=timezone.now(),
            google_campaign_id="555",
            google_resources={"campaign": f"customers/{CID}/campaigns/555"},
            ads_final_url="",
        )
        ok = (200, {"mutateOperationResponses": [{"campaignResult": {}}]})
        fake = self.google(TOKEN_OK, ok, ok)
        self.post("pause")
        self.post("resume")
        for n in (1, 2):
            text = str(fake.body(n))
            self.assertNotIn("finalUrl", text)
            self.assertNotIn("adGroupAd", text)
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        # En kampanj från före: annonserna leder kvar till kampanjens adress.
        self.assertEqual(campaign.ads_final_url, "")
        self.assertEqual(exports.ads_url(campaign), exports.landing_page_url(campaign))

    def test_publish_by_hand_stores_the_editor_files_address(self):
        with override_settings(**NOTHING):
            self.post()
        campaign = self.reload()
        self.assertEqual(campaign.status, Campaign.STATUS_LIVE)
        self.assertEqual(campaign.ads_final_url, self.own())
        csv_text = exports.google_ads_editor_csv(campaign)
        self.assertIn(self.own(), csv_text)
        self.assertIn(f"kampanj={campaign.pk}", csv_text)

    def test_the_review_page_shows_where_the_ads_lead(self):
        response = self.review_page()
        self.assertContains(response, f"{self.own()}?kampanj={self.campaign.pk}")


# ---------------------------------------------------------------------------
# Fliken Sidan
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class CampaignTabTests(SourcesFixture, TestCase):
    def tab(self):
        client = self.client_for(self.anna)
        url = reverse("flamingo:app_campaign", args=[self.campaign.pk])
        response = client.get(f"{url}?flik=sidan")
        self.assertEqual(response.status_code, 200)
        return response

    def test_a_published_campaign_shows_the_own_address(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(
            ads_final_url=exports.landing_page_url(self.page)
        )
        response = self.tab()
        self.assertContains(response, "Annonserna leder till")
        self.assertContains(
            response, f"adx.se/lp/exempelror/avloppsspolning/?kampanj={self.campaign.pk}"
        )
        self.assertContains(response, "räknas på Avloppsspolning Nacka")
        self.assertNotContains(response, "leder fortfarande till sidan")

    def test_an_unpublished_campaign_says_where_the_ads_will_lead(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(status=Campaign.STATUS_DRAFT)
        response = self.tab()
        self.assertContains(response, "Annonserna kommer att leda till")
        self.assertContains(
            response, f"adx.se/lp/exempelror/avloppsspolning/?kampanj={self.campaign.pk}"
        )

    def test_an_older_campaign_still_leads_to_the_campaign_address(self):
        response = self.tab()
        self.assertContains(response, f"adx.se/lp/{self.campaign.page_slug}/")
        self.assertContains(response, "publicerades innan annonserna gick till sidans egen")

    def test_a_page_switch_after_publishing_is_explained_and_the_agency_told(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(
            ads_final_url=exports.landing_page_url(self.page)
        )
        second = LandingPage.objects.create(
            account=self.account,
            name="Stopp i avloppet",
            draft={"blocks": self.page.draft_blocks},
            published={"blocks": self.page.published_blocks},
            published_at=timezone.now(),
        )
        with mock.patch.object(pagebuilder.pages, "page_problems", return_value=[]):
            pagebuilder.use_shared_page(self.reload_campaign(), second, user=self.anna)
        response = self.tab()
        self.assertContains(response, "leder fortfarande till sidan Avloppsspolning")
        self.assertContains(
            response, f"adx.se/lp/exempelror/avloppsspolning/?kampanj={self.campaign.pk}"
        )
        alert = "\n".join(m.body for m in mail.outbox)
        self.assertIn("Annonserna hos Google leder fortfarande till", alert)
        self.assertIn(exports.landing_page_url(second), alert)
        # Ingen ändring hos kunden: larmet går bara till byrån.
        self.assertNotIn(self.anna.email, [a for m in mail.outbox for a in m.to])

    def reload_campaign(self):
        return Campaign.objects.get(pk=self.campaign.pk)
