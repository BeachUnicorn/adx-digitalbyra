"""
Flamingo nämner inte byråns leverantörer (Giovanni 2026-10-10).

    InboxSmsErrorTests      sms-raderna i inkorgen: före ändringen sparades
                            leverantörens råa fel i SmsLog.error ("46elks:
                            HTTP 401", "46elks: RuntimeError: ...") och
                            visades rakt ut på förfrågan. Nya rader får
                            sms.FAILED_*; gamla visas genom sms.shown_error,
                            som bara släpper igenom modulens egna texter.
    AiNoteTests             ett fel från AI-modellen (llm._friendly nämner
                            Bedrock, AWS och Anthropic) når aldrig kunden:
                            sidbyggaren och kampanjförslaget visar sina fasta
                            noter
    AiGuardTests            AI-texterna nämner inte AI-tjänsten eller ADX
                            leverantörer (HARD_RULES och Guard), utom ett namn
                            som står i företagets egna uppgifter
    FlamingoLogosMigrationTests  "Sms via 46elks" på /flamingo/ blir "Sms" i
                            databasen (website 0020), en gång och bara där

Vakten i apps/common/test_leverantorer.py håller texterna i koden rena.
"""

import json
from importlib import import_module
from unittest import mock

from django.apps import apps as django_apps
from django.test import TestCase
from django.urls import reverse

from apps.assistant import llm
from apps.common.providers import names_in
from apps.website.models import Block, BlockPage

from . import generator, sms
from .models import Fact, SmsLog
from .pagebuilder import ai as page_ai
from .test_inbox import InboxFixture
from .test_pagebuilder_ai import AIFixture

#: Felet llm.call ger när modellåtkomsten saknas: byråns text, med namnen.
MODEL_ERROR = llm._friendly(Exception("Model access is denied (aws-marketplace)"))


class InboxSmsErrorTests(InboxFixture, TestCase):
    def row(self, lead, status, error):
        return SmsLog.objects.create(
            account=self.account,
            lead=lead,
            kind=SmsLog.KIND_AUTOREPLY,
            to="+46701112233",
            body="Tack, vi hör av oss.",
            status=status,
            error=error,
        )

    def test_old_provider_errors_are_shown_as_a_plain_failure(self):
        lead = self.lead_for(name="Gammal Rad")
        failed = SmsLog.STATUS_FAILED
        self.row(lead, failed, "46elks: HTTP 401")
        self.row(lead, failed, "46elks: RuntimeError: 46elks svarade failed")
        self.row(lead, failed, "46elks: [SSL] certificate is not valid for 'api.46elks.com'")
        self.row(lead, failed, sms.FAILED_HTTP.format(code=503))
        self.row(lead, failed, sms.FAILED_UNREACHABLE)
        self.row(lead, SmsLog.STATUS_DISABLED, sms.NOTE_QUIET)
        response = self.client_for(self.anna).get(reverse("flamingo:app_lead", args=[lead.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "46elks")
        self.assertNotContains(response, "RuntimeError")
        self.assertContains(response, sms.FAILED_OTHER, count=3)
        self.assertContains(response, "Sms-tjänsten svarade med fel (HTTP 503).")
        self.assertContains(response, sms.FAILED_UNREACHABLE)
        self.assertContains(response, sms.NOTE_QUIET)
        # Raderna i databasen är orörda; bara visningen byts.
        self.assertEqual(SmsLog.objects.filter(lead=lead, error__startswith="46elks").count(), 3)

    def test_shown_error_keeps_the_modules_own_texts(self):
        for text in (sms.NOTE_DEMO, sms.NOTE_BAD_NUMBER, sms.NOTE_DAILY_LIMIT, ""):
            with self.subTest(text=text):
                self.assertEqual(sms.shown_error(SmsLog(error=text)), text)
        self.assertEqual(sms.shown_error(SmsLog(error="ValueError: trasigt")), sms.FAILED_OTHER)


class AiNoteTests(AIFixture, TestCase):
    def test_the_model_error_names_providers_for_the_agency(self):
        self.assertIn("bedrock", names_in(MODEL_ERROR))

    def test_the_page_builder_shows_its_own_note(self):
        self.ai_on(side_effect=llm.ModelUnavailable(MODEL_ERROR))
        result = page_ai.build(self.page, self.account, goal="call", service=self.jour)
        self.assertEqual((result["source"], result["note"]), ("mallar", page_ai.NOTE_ERROR))
        self.assertEqual(names_in(json.dumps(result, ensure_ascii=False, default=str)), [])

    def test_the_campaign_proposal_shows_its_own_note(self):
        with (
            mock.patch.object(generator, "ai_available", return_value=(True, "")),
            mock.patch.object(generator, "ai_input", return_value={}),
            mock.patch("apps.assistant.llm.call", side_effect=llm.ModelUnavailable(MODEL_ERROR)),
        ):
            titles, descriptions, note = generator.ai_texts({})
        self.assertEqual((titles, descriptions), (None, None))
        self.assertEqual(note, "AI svarade inte, så texterna bygger på mallar.")

    def test_no_fixed_note_names_a_provider(self):
        for module in (page_ai, generator):
            for name in dir(module):
                value = getattr(module, name)
                if name.startswith("NOTE_") and isinstance(value, str):
                    with self.subTest(note=f"{module.__name__}.{name}"):
                        self.assertEqual(names_in(value), [])


class AiGuardTests(AIFixture, TestCase):
    RULE = "Inga namn på AI-tjänsten eller ADX leverantörer."

    def guard(self):
        return page_ai.make_base(self.page, self.account, service=self.jour).guard

    def test_the_hard_rules_forbid_the_names(self):
        rule = "Nämn aldrig AI-modellen, AI-tjänsten eller ADX leverantörer vid namn."
        self.assertIn(rule, page_ai.HARD_RULES)

    def test_texts_naming_the_ai_or_a_provider_are_stopped(self):
        guard = self.guard()
        for text in (
            "Texten är skriven av Claude.",
            "Sms via 46elks när någon frågar",
            "Sidan ligger hos AWS i Stockholm",
        ):
            with self.subTest(text=text):
                self.assertIn(self.RULE, guard.problems(text))
        # "S3" är en bilmodell i vanlig text, inte lagringstjänsten.
        self.assertNotIn(self.RULE, guard.problems("Service för Audi S3 i Nacka"))

    def test_a_name_in_the_companys_own_facts_may_stand(self):
        Fact.objects.create(
            account=self.account,
            key="kanal",
            label="Försäljning",
            value="Säljer reservdelar via Amazon",
            confirmed=True,
        )
        self.assertNotIn(self.RULE, self.guard().problems("Reservdelar via Amazon"))
        self.assertIn(self.RULE, self.guard().problems("Reservdelar via Amazon och AWS"))


class FlamingoLogosMigrationTests(TestCase):
    def test_the_item_is_replaced_once_and_nothing_else_changes(self):
        migration = import_module("apps.website.migrations.0020_inga_leverantorsnamn")
        page = BlockPage.objects.create(title="ADX Flamingo", slug="flamingo-test")
        logos = Block.objects.create(
            page=page,
            block_type="fl_logos",
            data={
                "label": "Det här kopplar ADX Flamingo ihop",
                "items": ["Google Ads", "Googles företagsuppgifter", "Sms via 46elks"],
            },
        )
        edited = Block.objects.create(
            page=page, block_type="fl_logos", data={"items": ["Sms via 46elks och mer"]}
        )
        prose = Block.objects.create(page=page, block_type="prose", data={"body": "Sms via 46elks"})
        for _ in range(2):
            migration.forwards(django_apps, None)
        logos.refresh_from_db()
        self.assertEqual(logos.data["items"], ["Google Ads", "Googles företagsuppgifter", "Sms"])
        self.assertEqual(logos.data["label"], "Det här kopplar ADX Flamingo ihop")
        edited.refresh_from_db()
        self.assertEqual(edited.data["items"], ["Sms via 46elks och mer"])
        prose.refresh_from_db()
        self.assertEqual(prose.data["body"], "Sms via 46elks")
