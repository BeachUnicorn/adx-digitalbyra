"""ADX Flamingo: regressionstester för granskningen av sidbyggaren
(2026-10-03). Varje test är ett fynd som granskarna visade, med samma
nummer som i granskningen: flödet (1-12) och säkerheten (S1-S9 och
härdningen). Ett test som fallerar betyder att felet är tillbaka."""

import copy
import json
import threading
import time
import warnings
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import alerts, checks, generator, media, pagebuilder, reviews
from .app_views import pages as page_views
from .models import Campaign, Fact, LandingPage, Service
from .pagebuilder import ai, render
from .test_media import image_bytes, logo_image, site_response, upload
from .test_pagebuilder_editor import ALERTS, EditorFixture
from .test_reviews import FakeGoogle, details
from .testing import migration

LONG_PHONE = "08-000 00 00 (vardagar) eller 070-000 00 00 (jour)"


def _fields(block):
    return pagebuilder.active_fields(block)


class FixFixture(EditorFixture):
    def create(self, client, **data):
        """En ny kampanj genom formuläret (Rörjour, ringer direkt)."""
        payload = {
            "service": str(self.jour.pk),
            "sales_mode": Service.SALES_CALL,
            "place": "Nacka",
            "radius_km": "15",
            "budget": "200",
            "budget_own": "",
        }
        payload.update(data)
        return client.post(reverse("flamingo:app_campaign_new"), payload)

    def price(self, service, value, order=10):
        return Fact.objects.create(
            account=self.account,
            key=generator.price_fact_key(service),
            label=f"Pris, {service}",
            value=value,
            confirmed=True,
            order=order,
        )

    def publish_unused(self):
        """Den oanvända sidan publicerad (kontrollerna gröna)."""
        pagebuilder.publish_page(self.unused)
        self.unused.refresh_from_db()
        return self.unused


# ---------------------------------------------------------------------------
# 1. Ett nytt förslag bygger bara om sin egen orörda sida
# ---------------------------------------------------------------------------


class ProposalRefreshTests(FixFixture, TestCase):
    def test_a_page_picked_for_a_new_campaign_is_never_rebuilt(self):
        before = copy.deepcopy(self.unused.draft_blocks)
        rev = self.unused.rev
        client = self.client_for(self.anna)
        self.create(client, page_choice="shared", shared_page=str(self.unused.pk))
        campaign = Campaign.objects.get(account=self.account, service=self.jour)
        self.assertEqual(campaign.landing_page_id, self.unused.pk)
        self.unused.refresh_from_db()
        self.assertEqual(self.unused.draft_blocks, before)
        self.assertEqual(self.unused.rev, rev)
        client.post(reverse("flamingo:app_campaign", args=[campaign.pk]), {"section": "regenerate"})
        self.unused.refresh_from_db()
        self.assertEqual(self.unused.draft_blocks, before)

    def test_the_proposal_rebuilds_only_its_own_untouched_page(self):
        client = self.client_for(self.anna)
        self.create(client)
        campaign = Campaign.objects.get(account=self.account, service=self.jour)
        page = campaign.landing_page
        self.assertEqual((page.built_for_id, page.built_rev), (campaign.pk, page.rev))
        url = reverse("flamingo:app_campaign", args=[campaign.pk])
        # Orörd: ett nytt förslag bygger om den (nya id:n), och den är
        # fortfarande förslagets.
        first_ids = [b["id"] for b in page.draft_blocks]
        client.post(url, {"section": "regenerate"})
        page.refresh_from_db()
        self.assertNotEqual([b["id"] for b in page.draft_blocks], first_ids)
        self.assertEqual(page.built_rev, page.rev)
        # Kunden lägger till ett block och byter variant, utan att skriva
        # något: sidan är kundens nu och byggs aldrig om.
        blocks = page.draft_blocks
        blocks.append(
            pagebuilder.new_block("faq", "three", self.account, ctx=pagebuilder.build_ctx(campaign))
        )
        blocks[0]["variant"] = "text"
        pagebuilder.save_draft(page, blocks, rev=page.rev, account=self.account)
        client.post(url, {"section": "regenerate"})
        page.refresh_from_db()
        self.assertIn("faq", [b["type"] for b in page.draft_blocks])
        self.assertEqual(page.draft_blocks[0]["variant"], "text")
        self.assertFalse(pagebuilder.is_untouched(page, campaign))

    def test_a_page_chosen_on_the_page_tab_is_never_rebuilt(self):
        client = self.client_for(self.anna)
        self.create(client)
        campaign = Campaign.objects.get(account=self.account, service=self.jour)
        own = campaign.landing_page
        url = reverse("flamingo:app_campaign", args=[campaign.pk])
        # Byt till den oanvända sidan och tillbaka till den egna: båda är
        # valda nu, och ingen av dem byggs om.
        client.post(url, {"section": "landing", "page": str(self.unused.pk)})
        client.post(url, {"section": "landing", "page": str(own.pk)})
        own.refresh_from_db()
        self.assertIsNone(own.built_for_id)
        before = copy.deepcopy(own.draft_blocks)
        client.post(url, {"section": "regenerate"})
        own.refresh_from_db()
        self.assertEqual(own.draft_blocks, before)

    def test_a_page_shared_by_another_campaign_is_never_rebuilt(self):
        a = Campaign.objects.create(account=self.account, service=self.jour, name="A", area="Nacka")
        page = pagebuilder.create_page_for_campaign(a)
        b = Campaign.objects.create(
            account=self.account, service=self.badrum, name="B", area="Nacka"
        )
        pagebuilder.use_shared_page(b, page)
        b.refresh_from_db()
        self.assertFalse(pagebuilder.refresh_from_proposal(b, None))
        self.assertFalse(pagebuilder.refresh_from_proposal(a, None))

    def test_an_accepted_template_suggestion_survives_a_new_proposal(self):
        client = self.client_for(self.anna)
        self.create(client)
        campaign = Campaign.objects.get(account=self.account, service=self.jour)
        page = campaign.landing_page
        result = ai.build(page, self.account, goal="call", service=self.jour, user=self.anna)
        response = self.post_json(
            client, self.url("app_page_save", page), {"rev": page.rev, "blocks": result["blocks"]}
        )
        self.assertEqual(response.status_code, 200, response.content)
        page.refresh_from_db()
        count = len(page.draft_blocks)
        client.post(reverse("flamingo:app_campaign", args=[campaign.pk]), {"section": "regenerate"})
        page.refresh_from_db()
        self.assertEqual(len(page.draft_blocks), count)


# ---------------------------------------------------------------------------
# 2. Ett långt telefonnummer fäller aldrig en kampanj
# ---------------------------------------------------------------------------


class LongPhoneTests(FixFixture, TestCase):
    def test_one_phone_takes_the_first_number_and_never_raises(self):
        self.assertEqual(generator.one_phone(LONG_PHONE), "08-000 00 00")
        self.assertEqual(generator.one_phone("Ring 070-123 45 67, kvällar"), "070-123 45 67")
        self.assertEqual(generator.one_phone("+46 8 000 00 00"), "+46 8 000 00 00")
        for value in ("", None, "Ring oss", "2026-10-03", "08 000 00 00 070 111 22 33"):
            self.assertEqual(generator.one_phone(value), "")

    def test_a_long_confirmed_phone_does_not_crash_a_new_campaign(self):
        Fact.objects.filter(account=self.account, key="telefon").update(value=LONG_PHONE)
        client = self.client_for(self.anna)
        client.raise_request_exception = False
        response = self.create(client)
        self.assertEqual(response.status_code, 302)
        campaign = Campaign.objects.get(account=self.account, service=self.jour)
        self.assertEqual(
            client.get(reverse("flamingo:app_campaign", args=[campaign.pk])).status_code, 200
        )
        blocks = campaign.landing_page.draft_blocks
        hero = next(b for b in blocks if b["type"] == "hero")
        self.assertEqual(_fields(hero)["phone"], "08-000 00 00")
        callbar = next(b for b in blocks if b["type"] == "callbar")
        self.assertEqual(_fields(callbar)["phone"], "08-000 00 00")
        self.assertEqual(generator.info_for(campaign).phone, "08-000 00 00")
        self.assertFalse(
            [p for p in pagebuilder.page_problems(campaign.landing_page) if "Telefon" in p.message]
        )
        # Mallarna i biblioteket tar samma nummer, aldrig en avhuggen text.
        hero = pagebuilder.new_block("hero", "call", self.account)
        self.assertEqual(_fields(hero)["phone"], "08-000 00 00")

    def test_an_old_invalid_version_never_makes_the_page_unsaveable(self):
        blocks = self.draft()
        hero = self.hero(blocks)
        old = copy.deepcopy(pagebuilder.active_version(hero))
        old["id"] = "v_GAMMALversio"
        old["fields"]["phone"] = LONG_PHONE  # längre än 40 tecken
        hero["versions"].insert(0, old)
        clean = pagebuilder.validate_blocks(blocks, account=self.account)
        clean_hero = self.hero(clean)
        self.assertNotIn("v_GAMMALversio", [v["id"] for v in clean_hero["versions"]])
        self.assertEqual(clean_hero["active"], hero["active"])
        pagebuilder.save_draft(self.unused, blocks, rev=self.unused.rev)
        # Den aktiva versionen stoppar fortfarande.
        hero["active"] = "v_GAMMALversio"
        with self.assertRaises(pagebuilder.BlockError):
            pagebuilder.validate_blocks(blocks, account=self.account)

    def test_the_migration_takes_one_number(self):
        page = {"title": "Rörjour i Nacka", "phone": LONG_PHONE}
        blocks = migration().blocks_for(page, "call", "Rörjour", "", "2026-10-03T10:00:00+00:00")
        pagebuilder.validate_blocks(blocks)
        self.assertEqual(_fields(blocks[0])["phone"], "08-000 00 00")
        blocks = migration().blocks_for({}, "call", "Rörjour", LONG_PHONE, "2026-10-03T10:00Z")
        self.assertEqual(_fields(blocks[0])["phone"], "08-000 00 00")
        blocks = migration().blocks_for({"phone": "ring oss"}, "call", "X", "", "2026-10-03T10:00Z")
        self.assertEqual(_fields(blocks[0])["phone"], "")


# ---------------------------------------------------------------------------
# 3. Pris och vanliga frågor bara med sidans tjänsts pris
# ---------------------------------------------------------------------------


class ServicePriceTests(FixFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.price("Rörjour", "Utryckning från 995 kr", order=1)
        self.price("Badrumsrenovering", "Från 45 000 kr", order=2)

    def test_the_starter_page_uses_its_own_service_price(self):
        blocks = page_views.starter_blocks(self.account, self.badrum)
        self.assertEqual(
            _fields(next(b for b in blocks if b["type"] == "price"))["price"], "Från 45 000 kr"
        )
        faq = next(b for b in blocks if b["type"] == "faq")
        answers = " ".join(i["a"] for i in _fields(faq)["items"])
        self.assertNotIn("995", answers)
        jour = page_views.starter_blocks(self.account, self.jour)
        price = next(b for b in jour if b["type"] == "price")
        self.assertEqual(_fields(price)["price"], "Utryckning från 995 kr")
        examples = pagebuilder.new_block(
            "price",
            "examples",
            self.account,
            ctx=pagebuilder.service_ctx(self.account, self.badrum),
        )
        self.assertEqual([i["price"] for i in _fields(examples)["items"]], ["Från 45 000 kr"])

    def test_a_library_block_on_a_page_uses_the_page_service_price(self):
        response = self.post_json(
            self.client_for(self.anna),
            self.url("app_page_block_new", self.shared),
            {"type": "price", "variant": "from"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(_fields(response.json()["block"])["price"], "Från 45 000 kr")

    def test_another_service_price_is_never_used(self):
        Fact.objects.filter(key=generator.price_fact_key("Badrumsrenovering")).delete()
        ctx = pagebuilder.service_ctx(self.account, self.badrum)
        with self.assertRaises(pagebuilder.BlockUnavailable):
            pagebuilder.new_block("price", "from", self.account, ctx=ctx)
        faq = pagebuilder.new_block("faq", "six", self.account, ctx=ctx)
        self.assertNotIn("995", json.dumps(_fields(faq)))
        self.assertFalse(pagebuilder.available(self.account, ctx=ctx)["price"][0])
        # Ett pris som inte hör till någon tjänst (timpris) får stå.
        Fact.objects.create(
            account=self.account,
            key="timpris",
            label="Timpris",
            value="Timpris 650 kr",
            confirmed=True,
        )
        price = pagebuilder.new_block(
            "price", "from", self.account, ctx=pagebuilder.service_ctx(self.account, self.badrum)
        )
        self.assertEqual(_fields(price)["price"], "Timpris 650 kr")


# ---------------------------------------------------------------------------
# 4 och 5. Ändringar som syns direkt på en live-sida larmar byrån
# ---------------------------------------------------------------------------


@override_settings(**ALERTS)
class LiveChangeAlertTests(FixFixture, TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox.clear()

    def test_a_palette_change_on_a_live_page_alerts_adx_never_the_customer(self):
        client = self.client_for(self.anna)
        response = self.post_json(
            client, self.url("app_page_settings", self.shared), {"palette": "red"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["larm@adx.example"])
        self.assertIn("Röd", mail.outbox[0].subject)
        self.assertIn("Kunden har inte mejlats", mail.outbox[0].body)
        # Samma palett igen ändrar inget och larmar inte; en sida som inte är
        # publicerad larmar aldrig.
        self.post_json(client, self.url("app_page_settings", self.shared), {"palette": "red"})
        self.post_json(client, self.url("app_page_settings", self.unused), {"palette": "green"})
        self.assertEqual(len(mail.outbox), 1)

    def test_a_new_or_removed_logo_alerts_adx(self):
        logo = media.add_upload(self.account, upload("logo.png", logo_image()))
        media.set_logo(logo, user=self.anna)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("logotyp", mail.outbox[0].subject)
        media.unset_logo(logo, user=self.anna)
        self.assertEqual(len(mail.outbox), 2)

    def test_switching_a_live_campaign_to_another_page_alerts_and_runs_the_checks(self):
        self.publish_unused()
        client = self.client_for(self.anna)
        url = reverse("flamingo:app_campaign", args=[self.live.pk])
        response = client.post(url, {"section": "landing", "page": str(self.unused.pk)})
        self.assertEqual(response.status_code, 302)
        self.live.refresh_from_db()
        self.assertEqual(self.live.landing_page_id, self.unused.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.unused.name, mail.outbox[0].subject)
        # En publicerad sida med problem nekas, och kampanjen står kvar.
        blocks = copy.deepcopy(self.shared.published_blocks)
        pagebuilder.active_version(self.hero(blocks))["fields"]["title"] = "Billigast i Nacka"
        bad = LandingPage.objects.create(
            account=self.account,
            name="Med problem",
            draft={"blocks": blocks},
            published={"blocks": blocks},
            published_at=timezone.now(),
        )
        with self.assertRaises(pagebuilder.PageProblems):
            pagebuilder.use_shared_page(self.live, bad)
        response = client.post(url, {"section": "landing", "page": str(bad.pk)}, follow=True)
        self.assertContains(response, "Kontrollerna hittade")
        self.live.refresh_from_db()
        self.assertEqual(self.live.landing_page_id, self.unused.pk)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_second_publish_within_the_hour_alerts_again(self):
        pagebuilder.publish_page(self.shared, self.anna)
        blocks = self.draft(self.shared)
        pagebuilder.add_version(
            self.hero(blocks),
            dict(_fields(self.hero(blocks)), title="Badrum i Nacka"),
            "customer",
            self.anna,
        )
        pagebuilder.save_draft(self.shared, blocks, rev=self.shared.rev, account=self.account)
        pagebuilder.publish_page(self.shared, self.anna)
        self.assertEqual(len(mail.outbox), 2)
        self.assertNotEqual(mail.outbox[0].subject, mail.outbox[1].subject)

    def test_a_demo_account_never_alerts(self):
        self.account.is_demo = True
        self.account.save(update_fields=["is_demo"])
        self.post_json(
            self.client_for(self.staff),
            self.url("app_page_settings", self.shared),
            {"palette": "red"},
        )
        self.assertEqual(mail.outbox, [])


# ---------------------------------------------------------------------------
# 6. "från X kr" bara när uppgiften säger "från"
# ---------------------------------------------------------------------------


class FromPriceTests(FixFixture, TestCase):
    def test_from_amount(self):
        cases = {
            "Utryckning från 995 kr": "995",
            "Från 45 000 kr": "45 000",
            "fr. 995 kr": "995",
            "Filmning från 1 900:-": "1 900",
            "Timpris 650 kr": "",
            "Timpris från 650 kr": "",
            "Från 650 kr/h": "",
            "Från 650 kr per timme": "",
            "Rabatt från 500 kr": "",
            "Pris per kvm från 2 000 kr": "",
            "995 kr": "",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(generator.from_amount(value), expected)

    def test_an_hourly_rate_never_becomes_a_from_price(self):
        self.price("Badrumsrenovering", "Timpris 650 kr")
        campaign = Campaign.objects.create(
            account=self.account, service=self.badrum, name="Badrum", area="Nacka + 15 km"
        )
        proposal = generator.build_proposal(campaign, save=False)
        texts = proposal.headlines + proposal.descriptions
        self.assertFalse([t for t in texts if "från 650" in t.lower()])
        base = ai.make_base(self.unused, self.account, service=self.badrum)
        self.assertEqual(base.price, "Timpris 650 kr")
        self.assertEqual(base.from_amount, "")
        self.assertNotIn("från_pris", ai._ai_payload(base, []))
        # Med "från" i uppgiften: från-priset i annonsen.
        self.price("Rörjour", "Utryckning från 995 kr")
        jour = Campaign.objects.create(
            account=self.account, service=self.jour, name="Jour", area="Nacka + 15 km"
        )
        proposal = generator.build_proposal(jour, save=False)
        self.assertTrue([h for h in proposal.headlines if "från 995 kr" in h.lower()])


# ---------------------------------------------------------------------------
# 8. Vem som skrev en version avgör servern
# ---------------------------------------------------------------------------


class AuthorshipTests(FixFixture, TestCase):
    def test_a_client_cannot_label_its_own_text_template_or_ai(self):
        client = self.client_for(self.anna)
        for n, source in enumerate(("template", "ai")):
            blocks = self.draft()
            hero = self.hero(blocks)
            forged = {
                "id": f"v_FORGEDforge{n}",
                "fields": dict(_fields(hero), title=f"Kundens egen text {n}"),
                "source": source,
                "by": self.staff.pk,
                "at": "2020-01-01T00:00:00+00:00",
                "sig": "0" * 32,
            }
            hero["versions"].append(forged)
            hero["active"] = forged["id"]
            response = self.post_json(
                client, self.url("app_page_save"), {"rev": self.unused.rev, "blocks": blocks}
            )
            self.assertEqual(response.status_code, 200, response.content)
            self.unused.refresh_from_db()
            saved = pagebuilder.active_version(self.hero(self.draft()))
            self.assertEqual((saved["source"], saved["by"]), ("customer", self.anna.pk))
            self.assertTrue(pagebuilder.is_signed(saved))
        self.assertFalse(pagebuilder.is_untouched(self.unused))

    def test_undo_after_use_the_suggestion_keeps_who_wrote_each_version(self):
        blocks = self.draft()
        hero = self.hero(blocks)
        pagebuilder.add_version(hero, dict(_fields(hero), title="ADX rubrik"), "adx", self.staff)
        pagebuilder.save_draft(self.unused, blocks, rev=self.unused.rev, account=self.account)
        self.unused.refresh_from_db()
        original = copy.deepcopy(self.unused.draft_blocks)
        client = self.client_for(self.anna)
        suggestion = page_views.starter_blocks(self.account, self.jour)
        response = self.post_json(
            client, self.url("app_page_save"), {"rev": self.unused.rev, "blocks": suggestion}
        )
        self.assertEqual(response.status_code, 200, response.content)
        # Förslaget är serverns, oförändrat: det står kvar som mallen.
        self.assertEqual({v["source"] for b in self.draft() for v in b["versions"]}, {"template"})
        response = self.post_json(
            client, self.url("app_page_save"), {"rev": response.json()["rev"], "blocks": original}
        )
        self.assertEqual(response.status_code, 200, response.content)
        restored = self.hero(self.draft())
        adx = next(v for v in restored["versions"] if v["fields"]["title"] == "ADX rubrik")
        self.assertEqual((adx["source"], adx["by"]), ("adx", self.staff.pk))

    def test_a_changed_server_version_becomes_the_editors(self):
        blocks = page_views.starter_blocks(self.account, self.jour)
        hero = self.hero(blocks)
        pagebuilder.active_version(hero)["fields"]["title"] = "Ändrad i webbläsaren"
        stamped = page_views.stamp_authorship(self.unused, blocks, self.staff, "adx")
        version = pagebuilder.active_version(self.hero(stamped))
        self.assertEqual((version["source"], version["by"]), ("adx", self.staff.pk))

    def test_a_new_block_from_the_editor_is_the_requesters_whatever_it_claims(self):
        response = self.post_json(
            self.client_for(self.anna),
            self.url("app_page_block_new"),
            {"type": "faq", "variant": "three", "fields": {"title": "Frågor"}, "source": "ai"},
        )
        version = pagebuilder.active_version(response.json()["block"])
        self.assertEqual(version["source"], "customer")


# ---------------------------------------------------------------------------
# 9. Problemen på den publicerade sidan syns där kunden rättar dem
# ---------------------------------------------------------------------------


class PublishedProblemTests(FixFixture, TestCase):
    def setUp(self):
        super().setUp()
        # Den publicerade versionen har ett nummer som inte längre är
        # bekräftat; utkastet är redan rättat.
        published = copy.deepcopy(self.shared.published_blocks)
        hero = self.hero(published)
        hero["variant"] = "call"
        pagebuilder.active_version(hero)["fields"]["phone"] = "08-999 99 99"
        LandingPage.objects.filter(pk=self.shared.pk).update(published={"blocks": published})
        self.shared.refresh_from_db()
        self.second.refresh_from_db()

    def test_submit_problems_say_they_are_on_the_published_page(self):
        problems = [p for p in checks.validate(self.second) if p.field == "page"]
        self.assertTrue(problems)
        self.assertTrue(all("publicerade sidan" in p.where for p in problems))
        self.assertTrue(all("Publicera" in p.message for p in problems))

    def test_the_page_tab_and_the_editor_show_them(self):
        client = self.client_for(self.anna)
        html = client.get(
            reverse("flamingo:app_campaign", args=[self.second.pk]) + "?flik=sidan"
        ).content.decode()
        self.assertIn("på den publicerade sidan", html)
        self.assertIn("Publicera det", html)
        response = client.get(self.url("app_page", self.shared))
        problems = response.context["config"]["problems"]
        self.assertTrue([p for p in problems if "publicerade sidan" in p["where"]])
        response = self.post_json(
            client,
            self.url("app_page_save", self.shared),
            {"rev": self.shared.rev, "blocks": self.shared.draft_blocks},
        )
        self.assertTrue([p for p in response.json()["problems"] if "publicerade" in p["where"]])


# ---------------------------------------------------------------------------
# 10. Migreringen 0011: frågornas nycklar klarar schemat
# ---------------------------------------------------------------------------


class MigrationKeyTests(TestCase):
    def test_long_and_duplicate_keys_are_reslugged_and_labels_kept(self):
        base = "a" * 40
        page = {
            "questions": [
                {"key": base, "label": "A?", "kind": "text"},
                {"key": f"{base}-2", "label": "B?", "kind": "text"},
                {"key": "Storlek på jobbet!", "label": "Hur stort?", "kind": "text"},
                {"key": "-_", "label": "Önskad dag", "kind": "date"},
            ]
        }
        blocks = migration().blocks_for(page, "quote", "Tak", "", "2026-10-03T10:00:00+00:00")
        pagebuilder.validate_blocks(blocks)
        questions = _fields(blocks[1])["questions"]
        self.assertEqual([q["label"] for q in questions], ["A?", "B?", "Hur stort?", "Önskad dag"])
        keys = [q["key"] for q in questions]
        self.assertEqual(len(set(keys)), 4)
        self.assertTrue(all(len(k) <= 40 for k in keys))
        self.assertEqual(keys[2], "storlek-pa-jobbet")


# ---------------------------------------------------------------------------
# 11 och S2. Google-profilen: valet står kvar, och profilen måste vara kundens
# ---------------------------------------------------------------------------


COMPETITOR = {
    "id": "ChIJCompetitor0001",
    "displayName": {"text": "Konkurrenten Rör AB"},
    "rating": 4.9,
    "userRatingCount": 812,
    "googleMapsUri": "https://maps.google.com/?cid=1",
    "reviews": details()["reviews"],
}


@override_settings(GOOGLE_PLACES_API_KEY="k", **ALERTS)
class GoogleProfileTests(FixFixture, TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox.clear()

    def confirm(self, place):
        with mock.patch.object(reviews, "urlopen", FakeGoogle(place=place)):
            return self.client_for(self.anna).post(
                reverse("flamingo:app_reviews"), {"action": "confirm", "place_id": place["id"]}
            )

    def test_a_competitors_profile_is_never_a_confirmed_rating(self):
        self.assertEqual(self.confirm(COMPETITOR).status_code, 302)
        self.account.refresh_from_db()
        self.assertTrue(self.account.google_place_unverified)
        self.assertEqual(self.account.google_rating, Decimal("4.9"))  # sparad, men inte visad
        self.assertIsNone(self.account.trusted_google_rating)
        fact = Fact.objects.get(account=self.account, key="betyg")
        self.assertFalse(fact.confirmed)
        self.assertNotIn("betyg", self.account.confirmed_facts())
        # Omdömena går att välja, men syns inte förrän profilen är intygad.
        ids = [r["id"] for r in self.account.google_reviews]
        reviews.select(self.account, ids)
        self.assertEqual(self.account.selected_google_reviews(), [])
        html = Client().get(self.live.landing_url).content.decode()
        self.assertNotIn("4,9", html)
        self.assertNotIn("Konkurrenten", html)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("liknar inte", mail.outbox[0].subject)
        # Kunden intygar: då syns det, och byrån larmas igen.
        response = self.client_for(self.anna).post(
            reverse("flamingo:app_reviews"), {"action": "own"}
        )
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertFalse(self.account.google_place_unverified)
        self.assertEqual(self.account.google_place_confirmed_by, self.anna)
        self.assertTrue(Fact.objects.get(account=self.account, key="betyg").confirmed)
        self.assertTrue(self.account.selected_google_reviews())
        self.assertEqual(len(mail.outbox), 2)
        # Cron hämtar samma profil igen: intyget står kvar.
        with mock.patch.object(reviews, "urlopen", FakeGoogle(place=COMPETITOR)):
            reviews.refresh_due(now=timezone.now() + reviews.REFRESH_EVERY * 2)
        self.account.refresh_from_db()
        self.assertFalse(self.account.google_place_unverified)

    def test_the_cron_path_checks_the_owner_too(self):
        self.account.google_place_id = COMPETITOR["id"]
        self.account.save(update_fields=["google_place_id"])
        with mock.patch.object(reviews, "urlopen", FakeGoogle(place=COMPETITOR)):
            summary = reviews.refresh_due()
        self.assertEqual(summary.fetched, 1)
        self.account.refresh_from_db()
        self.assertTrue(self.account.google_place_unverified)
        self.assertFalse(Fact.objects.get(account=self.account, key="betyg").confirmed)

    def test_the_customers_own_profile_is_confirmed_by_name_or_phone(self):
        own = dict(COMPETITOR, id="ChIJLindqvist00001", displayName={"text": "Lindqvist Rör AB"})
        self.confirm(own)
        self.account.refresh_from_db()
        self.assertFalse(self.account.google_place_unverified)
        self.assertTrue(Fact.objects.get(account=self.account, key="betyg").confirmed)
        self.assertEqual(mail.outbox, [])
        # Ett annat namn men samma telefonnummer som en bekräftad uppgift.
        by_phone = dict(COMPETITOR, id="ChIJAnnatNamn0001", nationalPhoneNumber="08 000 00 00")
        self.assertTrue(reviews._owned(by_phone, self.account))
        self.assertFalse(reviews._owned(COMPETITOR, self.account))

    def test_selected_reviews_missing_from_one_refresh_are_kept(self):
        own = dict(COMPETITOR, id="ChIJLindqvist00001", displayName={"text": "Lindqvist Rör AB"})
        reviews.store_details(self.account, own, own["id"])
        ids = [r["id"] for r in self.account.google_reviews]
        reviews.select(self.account, ids)
        # Google ger bara det ena omdömet den här gången.
        reviews.store_details(self.account, dict(own, reviews=own["reviews"][:1]), own["id"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_reviews_selected, ids)
        self.assertEqual(len(self.account.selected_google_reviews()), 1)
        reviews.store_details(self.account, own, own["id"])
        self.account.refresh_from_db()
        self.assertEqual(len(self.account.selected_google_reviews()), 2)


# ---------------------------------------------------------------------------
# 12. Logotypens färger följer logotypen
# ---------------------------------------------------------------------------


class LogoColorFallbackTests(FixFixture, TestCase):
    def test_a_colourless_or_removed_logo_clears_the_colours(self):
        logo = media.add_upload(self.account, upload("logo.png", logo_image()))
        media.set_logo(logo)
        LandingPage.objects.filter(pk=self.shared.pk).update(palette="logo")
        self.shared.refresh_from_db()
        self.assertTrue(self.shared.logo_colors.get("primary"))
        # En logotyp utan färger: inga gamla färger kvar, paletten blir blå.
        grey = media.add_upload(self.account, upload("grå.png", image_bytes(color=(255, 255, 255))))
        self.assertEqual(media.set_logo(grey), {})
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.logo_colors, {})
        self.assertEqual(self.shared.palette, LandingPage.PALETTE_BLUE)
        # Samma när logotypen tas bort.
        media.set_logo(logo)
        LandingPage.objects.filter(pk=self.shared.pk).update(palette="logo")
        media.unset_logo(logo)
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.logo_colors, {})
        self.assertEqual(self.shared.palette, LandingPage.PALETTE_BLUE)
        logo_option = next(p for p in page_views._palettes(self.shared) if p["key"] == "logo")
        self.assertFalse(logo_option["enabled"])


# ---------------------------------------------------------------------------
# S1. Bilderna avkodas aldrig så att minnet tar slut
# ---------------------------------------------------------------------------


class ImageMemoryTests(FixFixture, TestCase):
    def assert_refused_without_decoding(self, data):
        with (
            warnings.catch_warnings(),
            mock.patch.object(Image.Image, "load", side_effect=AssertionError("avkodad")),
            self.assertRaises(media.MediaError) as caught,
        ):
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            media.process_image(data)
        self.assertIn("för stor", caught.exception.message)

    def test_large_images_are_refused_from_the_header(self):
        # 25 miljoner bildpunkter i en PNG på några kB.
        self.assert_refused_without_decoding(image_bytes("PNG", (5000, 5000), 0, mode="L"))
        # En WebP får en tredjedel av gränsen (libwebp avkodar alltid allt).
        big_webp = image_bytes("WEBP", (3000, 3000), 0, mode="L", lossless=True)
        self.assert_refused_without_decoding(big_webp)
        self.assertLess(media.UPLOAD_MAX_PIXELS // media.WEBP_SHARE, 3000 * 3000)
        ok = media.process_image(image_bytes("WEBP", (2000, 2000), 0, mode="L", lossless=True))
        self.assertEqual((ok.width, ok.height), (2000, 2000))

    def test_a_large_jpeg_is_decoded_at_a_smaller_scale(self):
        data = image_bytes("JPEG", (6000, 4500), 128, mode="L")  # 27 MP
        processed = media.process_image(data)
        self.assertEqual(max(processed.width, processed.height), 2400)

    def test_uploads_per_hour(self):
        with mock.patch.object(media, "UPLOADS_PER_HOUR", 2):
            for n in range(2):
                media.add_upload(self.account, upload(f"bild{n}.png", image_bytes()))
            with self.assertRaises(media.MediaError) as caught:
                media.add_upload(self.account, upload("bild3.png", image_bytes()))
        self.assertIn("i timmen", caught.exception.message)

    def test_site_images_are_fetched_in_threads_but_decoded_one_at_a_time_here(self):
        decoded_in = []
        real = media._decode

        def spy(*args, **kwargs):
            decoded_in.append(threading.current_thread().name)
            return real(*args, **kwargs)

        class Page:
            images = [
                {"url": f"https://x.example/{n}.png", "kind": "img", "logo": False, "alt": ""}
                for n in range(4)
            ]

        def fetcher(url, **kwargs):
            shade = int(url.rsplit("/", 1)[1].split(".")[0]) * 40
            return site_response(url, image_bytes("PNG", (400, 300), (shade, 90, 200)), "image/png")

        with mock.patch.object(media, "_decode", side_effect=spy):
            job = media.SiteImageFetch.start(
                self.account, [Page()], deadline=time.monotonic() + 5, fetcher=fetcher
            )
            created = job.finish(self.account)
        self.assertEqual(created, 4)
        self.assertEqual(set(decoded_in), {threading.current_thread().name})
        # En stor bild hämtas men avkodas aldrig för en miniatyr.
        self.assertFalse(media._peek_ok(image_bytes("PNG", (4000, 3000), 0, mode="L")))


# ---------------------------------------------------------------------------
# S5. Certifikat och garanti bara med bekräftade uppgifter
# ---------------------------------------------------------------------------


class CertificateTests(FixFixture, TestCase):
    def certificates(self, items, title="Behörigheter"):
        return {
            "id": "b_CERTcertCERT",
            "type": "certificates",
            "variant": "badges",
            "active": "v_CERTcertCERT",
            "versions": [
                {
                    "id": "v_CERTcertCERT",
                    "fields": {"title": title, "items": items},
                    "source": "customer",
                    "by": None,
                    "at": timezone.now().isoformat(),
                }
            ],
        }

    def test_a_certificates_block_without_a_confirmed_fact_is_not_published(self):
        Fact.objects.filter(account=self.account, key="behorighet").delete()
        blocks = self.draft()
        blocks.insert(1, self.certificates([{"name": "Auktoriserad elinstallatör", "text": ""}]))
        pagebuilder.save_draft(self.unused, blocks, rev=self.unused.rev)
        with self.assertRaises(pagebuilder.PageProblems) as caught:
            pagebuilder.publish_page(self.unused)
        messages = [p.message for p in caught.exception.problems]
        self.assertIn(pagebuilder.registry.REQUIRES_TEXT["certificate_fact"], messages)

    def test_unconfirmed_certificates_and_claims_are_problems(self):
        blocks = self.draft()
        blocks.insert(
            1,
            self.certificates(
                [
                    {"name": "Säker Vatten-auktoriserade", "text": ""},
                    {"name": "Auktoriserad elinstallatör", "text": ""},
                    {"name": "Säker Vatten", "text": "Ansvarsförsäkrade"},
                ]
            ),
        )
        problems = pagebuilder.page_problems(self.unused, blocks=blocks)
        where = [(p.where, p.message) for p in problems if p.block == "b_CERTcertCERT"]
        self.assertEqual(len(where), 2, where)
        self.assertTrue(any("namn 2" in w for w, _ in where))
        self.assertTrue(any("Ansvarsförsäkrade" in m for _, m in where))
        # Mallens block klarar sina egna kontroller.
        template = pagebuilder.new_block("certificates", "badges", self.account)
        self.assertEqual(
            pagebuilder.page_problems(self.unused, blocks=[*self.draft(), template]), []
        )


# ---------------------------------------------------------------------------
# S6, S7, S8 och S9
# ---------------------------------------------------------------------------


class LimitAndRequestTests(FixFixture, TestCase):
    def test_at_most_50_pages_per_account(self):
        count = LandingPage.objects.filter(account=self.account).count()
        LandingPage.objects.bulk_create(
            LandingPage(account=self.account, name=f"Fyllnad {n}")
            for n in range(pagebuilder.MAX_PAGES - count)
        )
        client = self.client_for(self.anna)
        response = client.post(
            reverse("flamingo:app_page_new"), {"service": self.jour.pk}, follow=True
        )
        self.assertContains(response, f"redan {pagebuilder.MAX_PAGES} sidor")
        client.post(reverse("flamingo:app_page_copy", args=[self.unused.pk]))
        self.assertEqual(
            LandingPage.objects.filter(account=self.account).count(), pagebuilder.MAX_PAGES
        )
        campaign = Campaign.objects.create(account=self.account, service=self.jour, name="X")
        with self.assertRaises(pagebuilder.PageLimit):
            pagebuilder.create_page_for_campaign(campaign)
        response = self.create(client)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Välj en befintlig sida")

    def test_deeply_nested_json_is_400(self):
        client = self.client_for(self.anna)
        body = "[" * 30000 + "]" * 30000
        for name in ("app_page_save", "app_page_ai_rewrite", "app_page_settings"):
            response = client.post(self.url(name), data=body, content_type="application/json")
            self.assertEqual(response.status_code, 400, name)

    def test_publish_checks_the_revision(self):
        client = self.client_for(self.anna)
        seen = self.unused.rev
        blocks = self.draft()
        pagebuilder.active_version(self.hero(blocks))["fields"]["title"] = "Från en annan flik"
        pagebuilder.save_draft(self.unused, blocks, rev=seen)
        response = self.post_json(client, self.url("app_page_publish"), {"rev": seen})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["rev"], seen + 1)
        self.unused.refresh_from_db()
        self.assertFalse(self.unused.is_published)
        response = self.post_json(client, self.url("app_page_publish"), {"rev": seen + 1})
        self.assertEqual(response.status_code, 200, response.content)
        response = self.post_json(client, self.url("app_page_publish"), {"rev": "sju"})
        self.assertEqual(response.status_code, 400)

    def test_ids_with_a_trailing_newline_are_refused(self):
        blocks = self.draft()
        blocks[0]["id"] += "\n"
        with self.assertRaises(pagebuilder.BlockError):
            pagebuilder.validate_blocks(blocks, account=self.account)
        blocks = self.draft()
        version = pagebuilder.active_version(blocks[0])
        old_id = version["id"]
        version["id"] = old_id + "\n"
        blocks[0]["active"] = version["id"]
        with self.assertRaises(pagebuilder.BlockError):
            pagebuilder.validate_blocks(blocks, account=self.account)
        self.assertEqual(reviews.clean_place_id("ChIJexempel1234\n"), "ChIJexempel1234")
        self.assertIsNone(reviews.PLACE_ID_RE.fullmatch("ChIJexempel1234\n"))


# ---------------------------------------------------------------------------
# Härdningen: länkarna till Google och redigerarens dokument
# ---------------------------------------------------------------------------


class HardeningTests(FixFixture, TestCase):
    def test_google_links_are_checked_when_stored_and_when_drawn(self):
        for bad in (
            "https://evil.example\\@maps.google.com/",
            "https://evil.example@maps.google.com/",
            "https://maps.google.com:8443/",
            "http://maps.google.com/",
            "javascript:alert(1)",
            "https://google.com.evil.example/",
            'https://maps.google.com/"onmouseover=alert(1)',
        ):
            with self.subTest(url=bad):
                self.assertEqual(reviews.google_link(bad), "")
        self.assertEqual(
            reviews.google_link("https://www.google.com/maps/contrib/1"),
            "https://www.google.com/maps/contrib/1",
        )
        self.account.google_rating = Decimal("4.5")
        self.account.google_maps_uri = "https://evil.example\\@maps.google.com/"
        self.account.google_reviews = [
            {
                "id": "places/x/reviews/1",
                "author": "Anna",
                "author_uri": "https://evil.example\\@www.google.com/",
                "rating": 5,
                "text": "Bra",
                "uri": "javascript:alert(1)",
            }
        ]
        self.account.google_reviews_selected = ["places/x/reviews/1"]
        self.account.save()
        blocks = self.draft(self.shared)
        blocks.insert(1, pagebuilder.new_block("reviews_google", "cards", self.account))
        LandingPage.objects.filter(pk=self.shared.pk).update(published={"blocks": blocks})
        html = Client().get(self.live.landing_url).content.decode()
        self.assertIn("Anna", html)
        self.assertNotIn("evil.example", html)
        self.assertNotIn("javascript:", html)

    def test_the_editors_document_runs_no_scripts(self):
        response = self.client_for(self.anna).get(self.url("app_page", self.shared))
        canvas = response.context["canvas_html"]
        head = canvas.split("<head>", 1)[1]
        self.assertTrue(head.lstrip().startswith(render.EDITING_CSP))
        public = Client().get(self.live.landing_url).content.decode()
        self.assertNotIn("Content-Security-Policy", public)


class AccountAlertTests(TestCase):
    def test_account_alerts_are_capped_per_day(self):
        from apps.projects.models import Customer

        from .models import FlamingoAccount

        account = FlamingoAccount.objects.create(customer=Customer.objects.create(name="Larm AB"))
        with override_settings(**ALERTS):
            sent = [
                alerts.send_account_alert(account, f"Larm {n}", ["Rad"])
                for n in range(alerts.ACCOUNT_ALERTS_PER_DAY + 2)
            ]
        self.assertEqual(sum(sent), alerts.ACCOUNT_ALERTS_PER_DAY)
