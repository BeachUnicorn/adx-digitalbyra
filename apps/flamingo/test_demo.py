"""ADX Flamingo: demokunden (flamingo_demo) och spärrarna för demokonton.

Demot får ligga i produktion (beslut 2026-10-03). Därför:

    DemoCommandTests      kommandot: --prod, idempotent, ingen inloggning i drift
    DemoContentTests      data på varje sida, påhittade nummer, rena kontroller
    DemoLandingTests      /lp/ är 404 för alla utom byråns förhandsvisning
    DemoScanTests         hemsidan läses aldrig av
    DemoSmsTests          inga sms, oavsett inställningar och 46elks
    DemoConversionTests   affärerna exporteras aldrig
    DemoGoogleTests       byråns knappar och Google-modulerna når aldrig Google

Inget test går ut på nätet: Google, 46elks och hämtningen av hemsidor är
utbytta, och de flesta testerna kontrollerar just att de aldrig anropas.
"""

import importlib
import io
import json
import re
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import get_resolver, reverse

from apps.projects.access import VIEW_AS_KEY
from apps.projects.auth import contact_for_email
from apps.projects.models import Customer
from apps.utskick import access as utskick_access
from apps.utskick import optin
from apps.utskick.models import (
    Consent,
    ConsentLog,
    Contact,
    ContactList,
    Event,
    FieldDef,
    SignupForm,
    Suppression,
    Tag,
    UtskickSettings,
)

from . import checks, google_ads, pagebuilder, scan, sms
from .manage_review import queued_uploads
from .management.commands import flamingo_demo as demo
from .models import (
    Campaign,
    CampaignDayStats,
    ConversionUpload,
    Fact,
    FlamingoAccount,
    Lead,
    Review,
    SmsLog,
)
from .test_google_ads import CONFIGURED, TOKEN_OK

User = get_user_model()

ELKS = {
    "ELKS_API_USERNAME": "u-test",
    "ELKS_API_PASSWORD": "p-test",
    "ELKS_SENDER": "ADXFlamingo",
    "SMS_SEND_LIVE": True,
}
#: PTS serier för fiktiva nummer: 08-465 004 00-99 och 070-174 06 05-99.
FICTIONAL_PHONE = re.compile(r"^\+46(?:8465004\d\d|7017406(?:0[5-9]|[1-9]\d))$")


#: Demots bilder (mediaarkivet) hamnar här under testerna, aldrig i
#: MEDIA_ROOT: en testkörning ska inte lämna filer efter sig.
DEMO_MEDIA = tempfile.mkdtemp(prefix="flamingo-demo-")


def run_demo(*args):
    out = io.StringIO()
    with override_settings(MEDIA_ROOT=DEMO_MEDIA):
        call_command("flamingo_demo", *args, stdout=out)
    return out.getvalue()


def demo_account():
    return FlamingoAccount.objects.select_related("customer").get(is_demo=True)


def counts():
    account = demo_account()
    return {
        "kunder": Customer.objects.filter(name=demo.CUSTOMER_NAME).count(),
        "demokonton": FlamingoAccount.objects.filter(is_demo=True).count(),
        "användare": User.objects.count(),
        "uppgifter": account.facts.count(),
        "tjänster": account.services.count(),
        "kampanjer": account.campaigns.count(),
        "granskningar": Review.objects.filter(campaign__account=account).count(),
        "dagar": CampaignDayStats.objects.filter(campaign__account=account).count(),
        "förfrågningar": account.leads.count(),
        "konverteringar": ConversionUpload.objects.filter(lead__account=account).count(),
        "sms": account.sms_log.count(),
        # Kontakter och utskick (apps/utskick/demo.py).
        "utskick": UtskickSettings.objects.filter(account=account).count(),
        "kontakter": Contact.objects.filter(account=account).count(),
        "samtycken": Consent.objects.filter(contact__account=account).count(),
        "samtyckeslogg": ConsentLog.objects.filter(account=account).count(),
        "spärrar": Suppression.objects.filter(account=account).count(),
        "listor": ContactList.objects.filter(account=account).count(),
        "taggar": Tag.objects.filter(account=account).count(),
        "fält": FieldDef.objects.filter(account=account).count(),
        "händelser": Event.objects.filter(account=account).count(),
        "importer": account.utskick_imports.count(),
        "anmälningssidor": SignupForm.objects.filter(account=account).count(),
        "kopplade förfrågningar": account.leads.filter(contact__isnull=False).count(),
    }


class _Response:
    def __init__(self, body):
        self.status = 200
        self._raw = json.dumps(body).encode()

    def read(self, size=-1):
        return self._raw if size is None or size < 0 else self._raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GoogleRecorder:
    """Står i urlopens ställe i google_ads: svarar ja på allt och noterar
    varje anrop. Ett anrop som borde ha stoppats syns då i testet, i stället
    för att stanna vid ett fel som döljer det."""

    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if request.full_url.startswith(google_ads.TOKEN_URL):
            return _Response(TOKEN_OK[1])
        return _Response({"results": [], "resourceNames": [], "mutateOperationResponses": []})

    def urls(self):
        return [r.full_url for r in self.requests]

    def about_the_demo(self):
        """Anropen som nämner demokontots id eller dess klick-id."""
        hits = []
        for request in self.requests:
            text = request.full_url + (request.data or b"").decode("utf-8", "replace")
            if demo.GOOGLE_DIGITS in text or "demo-g" in text:
                hits.append(request.full_url)
        return hits


class DemoFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        run_demo("--prod")
        cls.account = demo_account()
        cls.customer = cls.account.customer
        cls.live = cls.account.campaigns.get(status=Campaign.STATUS_LIVE)
        cls.paused = cls.account.campaigns.get(status=Campaign.STATUS_PAUSED)
        cls.approved = cls.account.campaigns.get(
            status=Campaign.STATUS_NEEDS_CUSTOMER, approved_at__isnull=False
        )

    def staff_client(self, **kwargs):
        client = Client(**kwargs)
        client.force_login(self.staff)
        return client

    def demo_contact(self):
        """En kontakt hos demokunden (finns aldrig i drift, men om någon
        kopplar en ska den inte se mer än andra)."""
        user = User.objects.create_user("kontakt@exempelror.example", password="x")
        self.customer.users.add(user)
        client = Client()
        client.force_login(user)
        return user, client


# ---------------------------------------------------------------------------
# Kommandot
# ---------------------------------------------------------------------------


@override_settings(DEBUG=False)
class DemoCommandTests(TestCase):
    def test_refuses_without_the_flag_when_debug_is_off(self):
        with self.assertRaisesMessage(CommandError, "--prod"):
            run_demo()
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(FlamingoAccount.objects.exists())

    def test_running_twice_updates_and_never_duplicates(self):
        run_demo("--prod")
        first = counts()
        customer_pk = demo_account().customer_id
        run_demo("--prod")
        self.assertEqual(counts(), first)
        self.assertEqual(demo_account().customer_id, customer_pk)
        self.assertEqual(first["kunder"], 1)
        self.assertEqual(first["demokonton"], 1)
        self.assertGreater(first["förfrågningar"], 5)

    def test_staff_changes_to_the_demo_are_reset(self):
        run_demo("--prod")
        account = demo_account()
        account.leads.all().delete()
        account.is_enabled = False
        account.save()
        run_demo("--prod")
        account = demo_account()
        self.assertTrue(account.is_enabled)
        self.assertTrue(account.leads.exists())

    def test_production_creates_no_user_that_can_log_in(self):
        User.objects.create_user("byra", password="x12345678", is_staff=True)
        before = set(User.objects.values_list("pk", flat=True))
        output = run_demo("--prod")
        self.assertEqual(set(User.objects.values_list("pk", flat=True)), before)
        customer = demo_account().customer
        self.assertFalse(customer.users.exists())
        self.assertEqual(customer.email, "")
        self.assertIsNone(contact_for_email(demo.DEMO_CONTACT))
        self.assertIn("Ingen kontakt", output)

    def test_a_contact_from_a_local_run_is_switched_off_in_production(self):
        with override_settings(DEBUG=True):
            run_demo()
        contact = User.objects.get(username=demo.DEMO_CONTACT)
        self.assertIsNotNone(contact_for_email(demo.DEMO_CONTACT))
        stranger = User.objects.create_user("kopplad@example.com", email="kopplad@example.com")
        demo_account().customer.users.add(stranger)

        run_demo("--prod")
        contact.refresh_from_db()
        self.assertFalse(contact.is_active)
        self.assertFalse(contact.has_usable_password())
        self.assertFalse(demo_account().customer.users.exists())
        self.assertIsNone(contact_for_email(demo.DEMO_CONTACT))
        # Andra kopplade användare kopplas bara bort, de rörs inte.
        stranger.refresh_from_db()
        self.assertTrue(stranger.is_active)

    @override_settings(DEBUG=True)
    def test_locally_there_is_a_contact_without_a_password(self):
        run_demo()
        output = run_demo()
        contact = User.objects.get(username=demo.DEMO_CONTACT)
        self.assertFalse(contact.has_usable_password())
        self.assertFalse(contact.is_staff)
        self.assertEqual(list(demo_account().customer.users.all()), [contact])
        self.assertEqual(User.objects.filter(username=demo.DEMO_CONTACT).count(), 1)
        self.assertIn("/flamingo/app/", output)
        # Kontakten ser översikten med demodatan.
        client = Client()
        client.force_login(contact)
        self.assertContains(client.get(reverse("flamingo:app")), "Godkänn Badrumsrenovering")

    def test_a_real_customer_with_the_same_name_is_never_touched(self):
        real = Customer.objects.create(name=demo.CUSTOMER_NAME, email="riktig@example.com")
        account = FlamingoAccount.objects.create(customer=real, is_enabled=True)
        Fact.objects.create(account=account, key="telefon", label="Telefon", value="1")
        with self.assertRaises(CommandError):
            run_demo("--prod")
        real.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(real.email, "riktig@example.com")
        self.assertFalse(account.is_demo)
        self.assertEqual(account.facts.count(), 1)

    def test_another_account_marked_as_demo_is_left_alone(self):
        other = Customer.objects.create(name="Testkund AB")
        account = FlamingoAccount.objects.create(customer=other, is_demo=True)
        Fact.objects.create(account=account, key="telefon", label="Telefon", value="1")
        run_demo("--prod")
        self.assertEqual(account.facts.count(), 1)
        self.assertEqual(FlamingoAccount.objects.filter(is_demo=True).count(), 2)

    def test_the_command_sends_nothing_anywhere(self):
        with (
            override_settings(**CONFIGURED, **ELKS),
            mock.patch.object(google_ads, "urlopen") as google,
            mock.patch.object(sms, "urlopen") as elks,
            mock.patch.object(scan, "fetch") as fetch,
        ):
            run_demo("--prod")
        google.assert_not_called()
        elks.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(mail.outbox, [])


# ---------------------------------------------------------------------------
# Innehållet
# ---------------------------------------------------------------------------


class DemoContentTests(DemoFixture, TestCase):
    def test_the_account_is_a_demo_with_flamingo_on(self):
        self.assertTrue(self.account.is_demo)
        self.assertTrue(self.account.is_enabled)
        self.assertTrue(self.customer.is_active)
        self.assertEqual(self.account.enabled_by, self.staff)

    def test_the_company_is_clearly_fictional(self):
        self.assertIn("(demo)", self.customer.name)
        self.assertTrue(self.customer.website.endswith(".example"))
        self.assertTrue(self.account.website_url.endswith(".example"))
        self.assertEqual(self.customer.email, "")
        email = self.account.facts.get(key="epost").value
        self.assertTrue(email.endswith(".example"), email)
        for lead in self.account.leads.exclude(email=""):
            self.assertRegex(lead.email, r"@(?:example\.com|[\w.-]+\.example)$")

    def test_every_phone_number_is_reserved_for_fiction(self):
        numbers = [self.customer.phone, self.account.notify_phone]
        numbers += list(self.account.facts.filter(key="telefon").values_list("value", flat=True))
        for page in self.account.landing_pages.all():
            for block in page.draft_blocks:
                numbers.append(pagebuilder.active_fields(block).get("phone") or "")
        numbers += list(self.account.leads.exclude(phone="").values_list("phone", flat=True))
        numbers += list(self.account.sms_log.exclude(to="").values_list("to", flat=True))
        for text in [self.account.autoreply_text]:
            numbers += re.findall(r"0\d[\d -]{6,}\d", text)
        self.assertGreater(len(numbers), 10)
        for number in numbers:
            if not number:
                continue
            self.assertRegex(sms.normalize_phone(number) or number, FICTIONAL_PHONE)

    def test_campaigns_in_every_state(self):
        campaigns = list(self.account.campaigns.all())
        self.assertEqual(
            {c.status for c in campaigns}, {value for value, _ in Campaign.STATUS_CHOICES}
        )
        # Godkänd av kunden, inte publicerad (byråns "Godkänd, ej publicerad").
        self.assertIsNone(self.approved.published_at)
        self.assertIsNone(self.approved.pending_review())
        self.assertEqual(
            self.account.campaigns.filter(status=Campaign.STATUS_NEEDS_CUSTOMER).count(), 2
        )
        self.assertTrue(self.live.landing_url.startswith("/lp/"))

    def test_review_rounds_with_changes_and_reasons(self):
        reviews = Review.objects.filter(campaign__account=self.account)
        self.assertTrue(reviews.filter(state=Review.STATE_PENDING, round=2).exists())
        changes = [c for r in reviews for c in r.changes]
        self.assertGreaterEqual(len(changes), 5)
        for change in changes:
            self.assertTrue(change["reason"], change)
            self.assertTrue(change["before"] or change["after"], change)
        self.assertTrue(reviews.filter(state=Review.STATE_DONE, changes=[]).exists())

    def test_leads_in_every_status_with_a_call_click_and_a_won_deal(self):
        leads = self.account.leads.all()
        self.assertEqual({lead.status for lead in leads}, {v for v, _ in Lead.STATUS_CHOICES})
        self.assertEqual({lead.source for lead in leads}, {v for v, _ in Lead.SOURCE_CHOICES})
        click = leads.get(source=Lead.SOURCE_CALL_CLICK)
        self.assertEqual(click.display_name, "Klick på telefonnumret")
        self.assertTrue(click.has_click_id)
        won = leads.filter(status=Lead.STATUS_WON)
        self.assertEqual(won.count(), 2)
        deal = ConversionUpload.objects.get(lead__account=self.account)
        self.assertEqual(deal.kind, ConversionUpload.KIND_DEAL)
        self.assertEqual(deal.status, ConversionUpload.STATUS_QUEUED)
        self.assertEqual(deal.value_kr, deal.lead.value_kr)
        self.assertTrue(leads.exclude(gbraid="").exists())

    def test_sms_rows_show_sent_and_stopped(self):
        rows = self.account.sms_log.all()
        self.assertEqual({r.status for r in rows}, {SmsLog.STATUS_SENT, SmsLog.STATUS_DISABLED})
        self.assertEqual(
            {r.error for r in rows.filter(status=SmsLog.STATUS_DISABLED)},
            {sms.NOTE_QUIET, sms.NOTE_NO_MOBILE},
        )
        self.assertTrue(all(r.lead_id for r in rows))

    def test_facts_from_every_source_and_some_to_confirm(self):
        facts = self.account.facts.all()
        self.assertEqual({f.source for f in facts}, {v for v, _ in Fact.SOURCE_CHOICES})
        self.assertTrue(facts.filter(confirmed=False).exists())
        # Betyget från hemsidan visar varför det aldrig används.
        self.assertTrue(any(not f.is_usable for f in facts))
        self.assertIn("betyg", self.account.confirmed_facts())

    def test_google_figures_for_the_published_campaigns(self):
        self.assertTrue(self.live.day_stats.count() >= 28)
        self.assertTrue(self.paused.day_stats.exists())
        self.assertFalse(
            CampaignDayStats.objects.filter(campaign__account=self.account)
            .exclude(campaign__in=[self.live, self.paused])
            .exists()
        )
        for row in self.live.day_stats.all():
            self.assertLessEqual(row.cost_kr, self.live.daily_budget_kr)
            self.assertGreater(row.impressions, row.clicks)
        self.assertTrue(self.account.google_ready)
        self.assertEqual(self.account.google_billing_status, "APPROVED")

    def test_submitted_content_passes_the_checks(self):
        """Allt som skickats in klarade kontrollerna (annars hade inskicket
        stoppats); bara utkastet visar ett problem."""
        for campaign in self.account.campaigns.exclude(status=Campaign.STATUS_DRAFT):
            self.assertEqual(checks.validate(campaign), [], campaign.name)
            for review in campaign.reviews.all():
                submitted = Campaign(account=self.account, service=campaign.service)
                for field in Campaign.CONTENT_FIELDS:
                    if field in review.snapshot:
                        setattr(submitted, field, review.snapshot[field])
                # Sidan ligger i sidbyggaren och prövas med kampanjen ovan.
                submitted.landing_page = campaign.landing_page
                self.assertEqual(checks.validate(submitted), [], f"{campaign.name} {review}")
        draft = self.account.campaigns.get(status=Campaign.STATUS_DRAFT)
        self.assertTrue(checks.validate(draft))

    def test_every_tool_page_renders_for_staff_viewing_as_the_customer(self):
        client = self.staff_client()
        session = client.session
        session[VIEW_AS_KEY] = self.customer.pk
        session.save()
        names = [
            "flamingo:app",
            "flamingo:app_proposal",
            "flamingo:app_business",
            "flamingo:app_google",
            "flamingo:app_settings",
            "flamingo:app_campaigns",
            "flamingo:app_inbox",
            # Kontakter (apps/utskick), på för demot.
            "flamingo:app_contacts",
            "flamingo:app_contact_new",
            "flamingo:app_lists",
            "flamingo:app_import",
            "flamingo:app_fields",
            "flamingo:app_signup",
            "flamingo:app_contacts_settings",
            "flamingo:app_dpa",
            "flamingo:app_contacts_prune",
        ]
        urls = [reverse(name) for name in names]
        campaigns = self.account.campaigns.all()
        urls += [reverse("flamingo:app_campaign", args=[c.pk]) for c in campaigns]
        urls += [reverse("flamingo:app_lead", args=[x.pk]) for x in self.account.leads.all()]
        kontakter = Contact.objects.filter(account=self.account)
        urls += [reverse("flamingo:app_contact", args=[k.pk]) for k in kontakter]
        urls += [reverse("flamingo:app_contact_edit", args=[k.pk]) for k in kontakter]
        lists = ContactList.objects.filter(account=self.account)
        urls += [reverse("flamingo:app_list", args=[x.pk]) for x in lists]
        urls += [
            reverse("flamingo:app_import_job", args=[j.pk])
            for j in self.account.utskick_imports.all()
        ]
        for url in urls:
            self.assertEqual(client.get(url).status_code, 200, url)

    def test_staff_pages_render(self):
        client = self.staff_client()
        urls = [
            reverse("manage:flamingo_overview"),
            reverse("manage:flamingo_queue"),
            reverse("manage:customer_detail", args=[self.customer.pk]),
            reverse("manage:utskick_overview"),
            reverse("manage:utskick_customer_end", args=[self.customer.pk]),
        ]
        urls += [
            reverse("manage:flamingo_review", args=[c.pk]) for c in self.account.campaigns.all()
        ]
        for url in urls:
            self.assertEqual(client.get(url).status_code, 200, url)


class DemoStaffViewTests(DemoFixture, TestCase):
    def test_one_campaign_was_sent_without_review(self):
        # Granskningen är kundens val (beslut 2026-10-03): demot visar den
        # vanligaste vägen, inskicket utan granskning.
        self.assertFalse(self.approved.review_requested)
        self.assertFalse(self.approved.reviews.exists())
        self.assertIsNotNone(self.approved.approved_at)
        queue = self.staff_client().get(reverse("manage:flamingo_queue") + "?demo=1")
        to_publish = queue.content.decode().split('id="att-publicera"')[1]
        self.assertIn("utan granskning", to_publish)
        # Orsaken i kön är demot, inte att API:t saknas.
        self.assertIn("Demokontot publiceras aldrig hos Google.", to_publish)
        self.assertNotIn("Google Ads API är inte inkopplat", to_publish)

    def test_the_card_links_to_the_queue_with_the_demo_shown(self):
        card = self.staff_client().get(reverse("manage:customer_detail", args=[self.customer.pk]))
        self.assertContains(card, reverse("manage:flamingo_queue") + "?demo=1")

    def test_the_publish_panel_never_links_to_google_ads(self):
        page = self.staff_client().get(reverse("manage:flamingo_review", args=[self.live.pk]))
        self.assertContains(page, "Demokontot publiceras aldrig hos Google.")
        self.assertNotContains(page, "ads.google.com")
        self.assertNotContains(page, "Hos Google")


# ---------------------------------------------------------------------------
# Spärrarna
# ---------------------------------------------------------------------------


class DemoLandingTests(DemoFixture, TestCase):
    def test_every_demo_page_is_open_to_visitors(self):
        """Giovanni 2026-10-04: även demokontots sidor är öppna. De hittas
        inte (noindex, robots.txt) och visar ingen förhandsvisningsremsa."""
        thanks = reverse("flamingo_public:thanks", args=[self.live.page_slug])
        self.assertEqual(Client().get(thanks).status_code, 200)
        for campaign in self.account.campaigns.all():
            with self.subTest(campaign=campaign.name):
                response = Client().get(campaign.landing_url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
                self.assertFalse(response.context["preview"])

    def test_the_demo_customers_own_contact_sees_the_pages_like_anyone(self):
        _user, client = self.demo_contact()
        for campaign in self.account.campaigns.all():
            with self.subTest(campaign=campaign.name):
                response = client.get(campaign.landing_url)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["preview"])

    def test_a_click_on_the_number_is_never_counted_for_the_demo(self):
        """Klicken på numret räknas bara för live-kampanjer utanför demokontot
        (oförändrat 2026-10-04): sidan får ingen adress att skicka dem till."""
        before = Lead.objects.count()
        response = Client().get(self.live.landing_url)
        self.assertEqual(response.context["call_beacon"], "")
        click = reverse("flamingo_public:call_click", args=[self.live.page_slug])
        self.assertEqual(Client().post(click).status_code, 404)
        self.assertEqual(Lead.objects.count(), before)

    def test_staff_preview_works_and_creates_nothing(self):
        client = self.staff_client()
        response = client.get(self.live.landing_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["preview"])
        self.assertContains(response, "Förhandsvisning")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

        before = (Lead.objects.count(), SmsLog.objects.count())
        with mock.patch.object(sms, "urlopen") as elks:
            response = client.post(
                self.live.landing_url, {"name": "Test", "phone": "070-174 06 50"}
            )
        self.assertRedirects(
            response,
            reverse("flamingo_public:thanks", args=[self.live.page_slug]),
            fetch_redirect_response=False,
        )
        self.assertEqual((Lead.objects.count(), SmsLog.objects.count()), before)
        elks.assert_not_called()

    def test_staff_viewing_as_the_customer_still_previews(self):
        client = self.staff_client()
        session = client.session
        session[VIEW_AS_KEY] = self.customer.pk
        session.save()
        self.assertEqual(client.get(self.live.landing_url).status_code, 200)

    def test_the_flag_is_what_stops_the_counting(self):
        click = reverse("flamingo_public:call_click", args=[self.live.page_slug])
        self.assertEqual(Client().post(click).status_code, 404)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=False)
        self.assertEqual(Client().post(click).status_code, 204)


class DemoScanTests(DemoFixture, TestCase):
    def test_scan_website_refuses_without_fetching_or_changing_the_account(self):
        before = FlamingoAccount.objects.values().get(pk=self.account.pk)
        with mock.patch.object(scan, "fetch") as fetch:
            result = scan.scan_website(self.account, "https://exempelror.example")
        fetch.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, scan.DEMO_REFUSED)
        after = FlamingoAccount.objects.values().get(pk=self.account.pk)
        self.assertEqual(after, before)

    def test_the_button_says_why_and_uses_no_scan(self):
        _user, client = self.demo_contact()
        url = reverse("flamingo:app_proposal")
        with mock.patch.object(scan, "fetch") as fetch:
            response = client.post(
                url, {"action": "scan", "website_url": "https://exempelror.example"}
            )
        self.assertRedirects(response, url, fetch_redirect_response=False)
        fetch.assert_not_called()
        texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertEqual(texts, [scan.DEMO_REFUSED])
        self.account.refresh_from_db()
        self.assertEqual(self.account.scan_count, 0)
        self.assertEqual(self.account.scan_status, FlamingoAccount.SCAN_DONE)

    def test_other_accounts_can_still_scan(self):
        self.assertEqual(scan.demo_refusal(FlamingoAccount(is_demo=False)), "")


@override_settings(**ELKS)
class DemoUtskickTests(DemoFixture, TestCase):
    """Kontakter och utskick i demot (apps/utskick/demo.py)."""

    def test_utskick_is_on_and_the_demo_needs_no_dpa(self):
        self.assertTrue(utskick_access.is_enabled(self.account))
        self.assertTrue(utskick_access.dpa_ok(self.account))
        self.assertTrue(utskick_access.can_collect(self.account))
        self.assertIsNone(utskick_access.current_dpa())

    def test_every_number_and_address_is_fictional(self):
        for kontakt in Contact.objects.filter(account=self.account):
            if kontakt.phone:
                self.assertRegex(kontakt.phone, FICTIONAL_PHONE)
            if kontakt.email:
                self.assertRegex(kontakt.email, r"@[\w.-]*example(\.com)?$")

    def test_consent_in_every_state_with_proof(self):
        statuses = set(
            Consent.objects.filter(contact__account=self.account).values_list("status", flat=True)
        )
        for status in ("yes", "existing", "company", "pending", "missing", "declined"):
            self.assertIn(status, statuses)
        self.assertIn("unsubscribed", statuses)
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())
        self.assertTrue(
            ConsentLog.objects.filter(account=self.account, source="lp_form")
            .exclude(text_shown="")
            .exists()
        )

    def test_leads_with_a_ticked_box_are_linked_to_their_contact(self):
        linked = self.account.leads.filter(contact__isnull=False)
        self.assertGreaterEqual(linked.count(), 4)
        for lead in linked:
            self.assertEqual(lead.contact.account_id, self.account.pk)

    def test_the_pending_email_is_never_queued_for_the_demo(self):
        self.assertTrue(
            Consent.objects.filter(contact__account=self.account, status="pending").exists()
        )
        self.assertFalse(optin.due().filter(contact__account=self.account).exists())

    def test_the_signup_page_is_off(self):
        self.assertFalse(SignupForm.objects.get(account=self.account).is_active)

    def test_a_rerun_removes_suppressions_and_consent_logs_added_since(self):
        before = counts()
        kontakt = Contact.objects.filter(account=self.account, email__endswith=".example").first()
        from apps.utskick import suppression

        suppression.suppress(self.account, "email", kontakt.email, "manual", "manual")
        self.assertNotEqual(counts(), before)
        with override_settings(DEBUG=False):
            run_demo("--prod")
        self.assertEqual(counts(), before)

    def test_reset_refuses_a_real_account(self):
        from apps.utskick import demo as utskick_demo

        real = Customer.objects.create(name="Riktig AB")
        account = FlamingoAccount.objects.create(customer=real, is_enabled=True)
        with self.assertRaises(ValueError):
            utskick_demo.reset(account)


class DemoSmsTests(DemoFixture, TestCase):
    def test_a_new_lead_on_the_demo_sends_nothing(self):
        lead = Lead.objects.create(
            account=self.account,
            campaign=self.live,
            name="Test",
            phone="070-174 06 50",
        )
        with mock.patch.object(sms, "urlopen") as elks:
            rows = sms.notify_new_lead(lead)
        elks.assert_not_called()
        self.assertEqual(
            [(r.kind, r.status, r.error) for r in rows],
            [
                (SmsLog.KIND_OWNER, SmsLog.STATUS_DISABLED, sms.NOTE_DEMO),
                (SmsLog.KIND_AUTOREPLY, SmsLog.STATUS_DISABLED, sms.NOTE_DEMO),
            ],
        )

    def test_send_itself_refuses(self):
        with mock.patch.object(sms, "urlopen") as elks:
            row = sms.send(self.account, SmsLog.KIND_OWNER, "070-174 06 05", "Test")
        elks.assert_not_called()
        self.assertEqual((row.status, row.error), (SmsLog.STATUS_DISABLED, sms.NOTE_DEMO))
        self.assertEqual(sms.NOTE_DEMO, "Demokonto: inga sms skickas.")

    def test_the_demo_reason_wins_even_when_sms_is_switched_off(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            notify_sms=False, autoreply_enabled=False
        )
        lead = Lead.objects.create(account=self.account, name="Test", phone="070-174 06 50")
        with mock.patch.object(sms, "urlopen") as elks:
            rows = sms.notify_new_lead(lead)
        elks.assert_not_called()
        self.assertEqual({r.error for r in rows}, {sms.NOTE_DEMO})


class DemoConversionTests(DemoFixture, TestCase):
    def setUp(self):
        self.deal = ConversionUpload.objects.get(lead__account=self.account)

    def test_the_queued_deal_is_never_in_the_export(self):
        self.assertEqual(self.deal.status, ConversionUpload.STATUS_QUEUED)
        self.assertNotIn(self.deal, list(queued_uploads()))
        self.assertNotIn(self.deal, list(queued_uploads(self.customer.pk)))

        client = self.staff_client()
        url = reverse("manage:flamingo_conversions_csv")
        for params in ({}, {"kund": self.customer.pk}):
            response = client.get(url, params)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("demo-g", response.content.decode())

    def test_it_cannot_be_marked_as_exported(self):
        client = self.staff_client()
        client.post(reverse("manage:flamingo_conversions_csv"), {"upload": [self.deal.pk]})
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.status, ConversionUpload.STATUS_QUEUED)
        self.assertIsNone(self.deal.exported_at)

    def test_the_queue_does_not_count_it(self):
        response = self.staff_client().get(reverse("manage:flamingo_queue"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.deal, response.context["uploads"])


@override_settings(**CONFIGURED)
class DemoGoogleTests(DemoFixture, TestCase):
    """Med Google Ads API inkopplat: inget som rör demokontot får nå Google.

    Testerna går igenom det som finns (knapparna, Google-modulerna och
    synkkommandot) och fångar en regression där en ny väg glömmer is_demo
    (google_ads.ensure_not_demo)."""

    #: Byråns adresser som inte ska svepas: de ändrar bara aktiveringen eller
    #: kundvyn, och skulle stänga av demot för resten av svepet. Samma sak
    #: med utskickens del av kundkortet (en ruta som inte skickas är av).
    SKIP_ROUTES = {"flamingo_customer_update", "flamingo_view_as", "utskick_customer_update"}
    ACTIONS = ("", "publish", "pause", "resume", "link", "create", "sync", "upload")

    def setUp(self):
        cache.clear()
        self.google = GoogleRecorder()
        patcher = mock.patch.object(google_ads, "urlopen", self.google)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(cache.clear)

    def test_publish_pause_and_resume_never_reach_google(self):
        client = self.staff_client()
        for campaign, action in (
            (self.approved, "publish"),
            (self.live, "pause"),
            (self.paused, "resume"),
        ):
            response = client.post(
                reverse("manage:flamingo_publish", args=[campaign.pk]), {"action": action}
            )
            self.assertEqual(response.status_code, 302, action)
        self.assertEqual(self.google.urls(), [])

    def test_the_customer_card_buttons_never_reach_google(self):
        client = self.staff_client()
        url = reverse("manage:flamingo_google_account", args=[self.customer.pk])
        for action in ("link", "create", "sync"):
            client.post(url, {"action": action, "invite": "1", "invite_email": "x@example.com"})
        self.assertEqual(self.google.urls(), [])

    def _flamingo_routes(self):
        """Byråns Flamingo- och utskicksadresser med ett id: (namn, mönster)."""
        manage = get_resolver().namespace_dict["manage"][1]
        for pattern in self._patterns(manage.url_patterns):
            name = getattr(pattern, "name", None) or ""
            converters = getattr(pattern.pattern, "converters", {})
            if name.startswith(("flamingo_", "utskick_")) and list(converters) == ["pk"]:
                if name not in self.SKIP_ROUTES:
                    yield name, str(pattern.pattern)

    def _patterns(self, patterns):
        """Mönstren, också de i include() utan eget namnrum (sms, utskick)."""
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                yield from self._patterns(pattern.url_patterns)
            else:
                yield pattern

    def test_no_staff_route_on_the_demo_reaches_google(self):
        """Varje Flamingo-adress i panelen med ett id, med demokundens,
        kampanjernas och förfrågningarnas id, GET och POST med de vanliga
        knapparna. En ny vy som glömmer is_demo fälls här."""
        campaigns = list(self.account.campaigns.values_list("pk", flat=True))
        leads = list(self.account.leads.values_list("pk", flat=True))
        client = self.staff_client(raise_request_exception=False)
        routes = list(self._flamingo_routes())
        self.assertIn("flamingo_publish", {name for name, _ in routes})
        self.assertIn("utskick_customer_end", {name for name, _ in routes})
        with (
            mock.patch.object(sms, "urlopen") as elks,
            mock.patch.object(scan, "fetch") as fetch,
        ):
            for name, route in routes:
                if "kund" in route:
                    ids = [self.customer.pk]
                elif "kampanj" in route or "granska" in route:
                    ids = campaigns
                else:
                    ids = [self.customer.pk, *campaigns, *leads]
                for pk in ids:
                    url = reverse(f"manage:{name}", args=[pk])
                    client.get(url)
                    for action in self.ACTIONS:
                        client.post(url, {"action": action} if action else {})
        self.assertEqual(self.google.urls(), [])
        elks.assert_not_called()
        fetch.assert_not_called()

    def _module(self, name):
        try:
            return importlib.import_module(f"apps.flamingo.{name}")
        except ImportError:
            self.skipTest(f"{name} finns inte än")

    def _try(self, func, *args, **kwargs):
        """Anropa och svälj felet: här räknas bara anropen till Google."""
        try:
            return func(*args, **kwargs)
        except Exception:  # noqa: BLE001 - ett nej till demot får vara ett fel
            return None

    def test_the_publish_module_skips_the_demo(self):
        publish = self._module("google_publish")
        self.assertFalse(publish.api_available(self.account))
        for name, campaign in (
            ("go_live", self.approved),
            ("pause", self.live),
            ("resume", self.paused),
        ):
            func = getattr(publish, name, None)
            if func is not None:
                self._try(func, campaign)
        self.assertEqual(self.google.urls(), [])

    def test_the_account_module_skips_the_demo(self):
        accounts = self._module("google_accounts")
        for name in ("request_link", "create_client_account", "sync_account_status"):
            func = getattr(accounts, name, None)
            if func is not None:
                self._try(func, self.account)
        self.assertEqual(self.google.urls(), [])

    def test_the_sync_command_never_asks_about_the_demo(self):
        """Synkkommandot går över alla konton: inget anrop får gälla demot
        (dess id eller klick-id), och demots rader ändras inte."""
        commands = sorted(
            name
            for name, app in get_commands().items()
            if app == "apps.flamingo" and "google" in name
        )
        if not commands:
            self.skipTest("Inget Google-kommando finns än")
        before = FlamingoAccount.objects.values().get(pk=self.account.pk)
        deal = ConversionUpload.objects.get(lead__account=self.account)
        for name in commands:
            try:
                call_command(name, stdout=io.StringIO(), stderr=io.StringIO())
            except (CommandError, SystemExit):
                pass
        self.assertEqual(self.google.about_the_demo(), [])
        after = FlamingoAccount.objects.values().get(pk=self.account.pk)
        self.assertEqual(after["google_synced_at"], before["google_synced_at"])
        deal.refresh_from_db()
        self.assertEqual(deal.status, ConversionUpload.STATUS_QUEUED)
