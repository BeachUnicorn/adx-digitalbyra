"""
Tester för AI-redaktören.

Tyngdpunkten ligger på säkerhetsgränsen: att ett förslag aldrig skriver, att
affärsdata inte kan klumpgodkännas, och att saneringen gäller AI-text lika
mycket som manuell inmatning. Det är de egenskaperna som gör att kunden vågar
koppla in en AI mot sin sajt.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import reversion
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from reversion.models import Version

from apps.areas.models import Area, AreaLevel
from apps.faq.models import FAQItem, FAQSection
from apps.services.models import Audience, Service, ServiceCategory, ServiceStep
from apps.website.models import Block, BlockPage

from . import draft, llm, tasks
from .asgi_app import _bearer_token
from .diffing import field_diffs
from .models import (
    AIJob,
    AssistantToken,
    ChatMessage,
    ChatRole,
    DraftChange,
    RevisionMeta,
    Risk,
)
from .oauth_models import DEFAULT_SCOPE, AuthorizationCode, OAuthClient, OAuthToken
from .operations import REGISTRY, OperationError


class BaseCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser("redaktor", "r@t.local", "hemligt")

        # Fixturerna skapas inuti en revision, precis som produktionen ser ut
        # efter `manage.py createinitialrevisions`. Utan den baslinjen finns
        # ingen tidigare version att återställa till.
        with reversion.create_revision():
            reversion.add_meta(RevisionMeta, source=RevisionMeta.Source.IMPORT)
            self.category = ServiceCategory.objects.create(name="Vatten")
            self.service = Service.objects.create(
                name="Byte av blandare", category=self.category, description="Gammal text"
            )
            self.region = Area.objects.create(name="Stockholms län", level=AreaLevel.REGION)
            self.area = Area.objects.create(
                name="Solna",
                level=AreaLevel.MUNICIPALITY,
                parent=self.region,
                intro="Gammalt intro",
            )
            self.page = BlockPage.objects.create(title="Om oss", slug="om-oss")

        self.job = AIJob.objects.create(user=self.user, title="Testjobb")


class RegistryTests(BaseCase):
    def test_every_operation_is_wellformed(self):
        """Registret är MCP:s verktygslista - ett trasigt schema syns först hos kunden."""
        for op in REGISTRY.values():
            with self.subTest(op=op.name):
                self.assertEqual(op.input_schema.get("type"), "object")
                self.assertIs(op.input_schema.get("additionalProperties"), False)
                self.assertTrue(op.description.strip())
                if op.risk == Risk.READ:
                    self.assertIsNotNone(op.read)
                else:
                    self.assertIsNotNone(op.prepare)
                    self.assertIsNotNone(op.apply)

    def test_no_operation_deletes(self):
        """Radering ligger utanför AI:ns yta - avaktivering är taket."""
        for name in REGISTRY:
            self.assertNotIn("radera", name)
            self.assertNotIn("ta_bort", name)


class ProposeTests(BaseCase):
    def test_propose_does_not_touch_the_database(self):
        draft.propose(self.job, "uppdatera_omrade_text", {"slug": "solna", "intro": "Nytt intro"})
        self.area.refresh_from_db()
        self.assertEqual(self.area.intro, "Gammalt intro")

    def test_propose_sanitises_ai_typography(self):
        """Em-dash och liknande ska bort - samma regel som för manuell text."""
        change = draft.propose(
            self.job,
            "uppdatera_omrade_text",
            {"slug": "solna", "intro": "Snabb hjälp — dygnet runt"},
        )
        self.assertNotIn("—", change.payload["intro"])
        self.assertIn("-", change.payload["intro"])

    def test_propose_keeps_template_variables(self):
        change = draft.propose(
            self.job,
            "uppdatera_omrade_text",
            {"slug": "solna", "intro": "Vi finns i {{ ort }}, ring {{ phone }}."},
        )
        self.assertIn("{{ ort }}", change.payload["intro"])
        self.assertIn("{{ phone }}", change.payload["intro"])

    def test_unknown_target_raises_operation_error(self):
        with self.assertRaises(OperationError):
            draft.propose(self.job, "uppdatera_omrade_text", {"slug": "finns-ej", "intro": "x"})

    def test_unknown_field_raises_operation_error(self):
        with self.assertRaises(OperationError):
            draft.propose(
                self.job, "uppdatera_tjanst_text", {"slug": self.service.slug, "hittepa": "x"}
            )

    def test_read_operation_cannot_be_proposed(self):
        with self.assertRaises(OperationError):
            draft.propose(self.job, "lista_tjanster", {})

    def test_nothing_becomes_public_without_approval(self):
        """
        Ett förslag får aldrig nå sajten av sig självt - det är hela
        säkerhetsgränsen. Godkännandet är kundens klick.
        """
        draft.propose(
            self.job,
            "skapa_tjanst",
            {"namn": "Avloppsspolning", "beskrivning": "Kort.", "steg": [{"rubrik": "Vi kommer"}]},
        )
        self.assertFalse(Service.objects.filter(name="Avloppsspolning").exists())

    def test_approved_service_is_live_and_complete(self):
        """
        Tjänsten skapas AKTIV. Den skapades tidigare inaktiv, vilket innebar
        att kunden godkände ett förslag och sedan fick leta upp tjänsten för
        att tända den. Eftersom arbetsgången nu är obligatorisk är det som
        godkänns en färdig tjänst, inte ett skal.
        """
        change = draft.propose(
            self.job,
            "skapa_tjanst",
            {
                "namn": "Avloppsspolning",
                "beskrivning": "Vi spolar avlopp.",
                "steg": [{"rubrik": "Felsökning"}, {"rubrik": "Spolning"}],
            },
        )
        draft.approve(change, self.user)
        service = Service.objects.get(name="Avloppsspolning")
        self.assertTrue(service.is_active)
        self.assertEqual(service.steps.count(), 2)

    def test_service_without_steps_is_refused(self):
        """Kravet ligger i schemat, men prepare måste hålla oavsett väg in."""
        with self.assertRaises(OperationError):
            draft.propose(
                self.job, "skapa_tjanst", {"namn": "Utan steg", "beskrivning": "X", "steg": []}
            )


class ApproveTests(BaseCase):
    def test_approve_writes_and_records_ai_revision(self):
        change = draft.propose(
            self.job, "uppdatera_omrade_text", {"slug": "solna", "intro": "Nytt intro"}
        )
        draft.approve(change, self.user)

        self.area.refresh_from_db()
        self.assertEqual(self.area.intro, "Nytt intro")

        version = Version.objects.get_for_object(self.area).first()
        self.assertEqual(version.revision.meta.source, RevisionMeta.Source.AI)
        self.assertEqual(version.revision.meta.job_id, self.job.pk)

    def test_reject_writes_nothing(self):
        change = draft.propose(
            self.job, "uppdatera_omrade_text", {"slug": "solna", "intro": "Nej tack"}
        )
        draft.reject(change, self.user)
        self.area.refresh_from_db()
        self.assertEqual(self.area.intro, "Gammalt intro")
        self.assertEqual(change.status, DraftChange.Status.REJECTED)

    def test_double_approve_is_blocked(self):
        change = draft.propose(
            self.job, "uppdatera_omrade_text", {"slug": "solna", "intro": "En gång"}
        )
        draft.approve(change, self.user)
        with self.assertRaises(OperationError):
            draft.approve(change, self.user)

    def test_approve_revalidates_against_current_state(self):
        """Objektet kan hinna försvinna mellan förslag och godkännande."""
        change = draft.propose(
            self.job, "uppdatera_tjanst_text", {"slug": self.service.slug, "description": "Ny"}
        )
        self.service.delete()
        with self.assertRaises(OperationError):
            draft.approve(change, self.user)

    def test_rejecting_cascades_to_dependants(self):
        first = draft.propose(
            self.job,
            "skapa_omrade",
            {"namn": "Sundbyberg", "niva": "kommun", "overordnad_slug": "stockholms-lan"},
        )
        second = draft.propose(
            self.job, "uppdatera_omrade_text", {"slug": "solna", "intro": "Beroende"}
        )
        second.depends_on = first
        second.save(update_fields=["depends_on"])

        draft.reject(first, self.user)
        second.refresh_from_db()
        self.assertEqual(second.status, DraftChange.Status.REJECTED)

    def test_undo_job_restores_previous_content(self):
        change = draft.propose(
            self.job, "uppdatera_omrade_text", {"slug": "solna", "intro": "Tillfälligt"}
        )
        draft.approve(change, self.user)
        draft.undo_job(self.job, self.user)

        self.area.refresh_from_db()
        self.assertEqual(self.area.intro, "Gammalt intro")


class ManageViewTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_bulk_approve_can_include_business_changes(self):
        """
        Kundens beslut 2026-08-21: den som granskar väljer själv vad hen
        godkänner, även affärsdata. Riskklassen är märkning, inte spärr.
        """
        change = draft.propose(
            self.job, "satt_tjanst_aktiv", {"slug": self.service.slug, "aktiv": False}
        )
        self.client.post(
            reverse("manage:assistant_job_bulk", args=[self.job.pk]),
            {"change_ids": [change.pk], "action": "approve"},
        )
        change.refresh_from_db()
        self.assertEqual(change.status, DraftChange.Status.APPLIED)
        self.service.refresh_from_db()
        self.assertFalse(self.service.is_active)

    def test_another_users_job_is_not_reachable(self):
        other = get_user_model().objects.create_user("annan", "a@t.local", "x")
        other_job = AIJob.objects.create(user=other, title="Andras jobb")
        self.assertEqual(self.client.get(f"/manage/ai/jobb/{other_job.pk}/").status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get("/manage/ai/")
        self.assertIn(response.status_code, (302, 403))

    def test_job_page_renders_diff(self):
        draft.propose(
            self.job, "uppdatera_omrade_text", {"slug": "solna", "intro": "Alldeles nytt"}
        )
        html = self.client.get(f"/manage/ai/jobb/{self.job.pk}/").content.decode()
        self.assertIn("Alldeles", html)
        self.assertIn("<ins>", html)


class TokenTests(BaseCase):
    def test_token_is_stored_hashed_and_authenticates(self):
        token, raw = AssistantToken.issue(self.user, "Min app")
        self.assertNotIn(raw, token.key_hash)
        self.assertEqual(AssistantToken.authenticate(raw), token)

    def test_revoked_token_stops_working(self):
        token, raw = AssistantToken.issue(self.user)
        token.is_active = False
        token.save(update_fields=["is_active"])
        self.assertIsNone(AssistantToken.authenticate(raw))

    def test_garbage_is_rejected(self):
        for value in ("", None, "inte-en-nyckel", "skvvs_fel"):
            self.assertIsNone(AssistantToken.authenticate(value))


class VersioningTests(BaseCase):
    def test_manual_manage_edit_creates_manual_revision(self):
        self.client.force_login(self.user)
        self.client.post(
            f"/manage/services/items/{self.service.pk}/",
            {
                "name": self.service.name,
                "category": self.category.pk,
                "description": "Manuellt ändrad",
                "body": "",
                "image": "",
                "labor_price_from": "",
                "labor_price_to": "",
                "material_price_from": "",
                "material_price_to": "",
                "faq_section": "",
                "order": 0,
                "is_active": "on",
            },
        )
        version = Version.objects.get_for_object(self.service).first()
        self.assertIsNotNone(version)
        self.assertEqual(version.revision.meta.source, RevisionMeta.Source.MANUAL)

    def test_history_page_and_revert(self):
        self.client.force_login(self.user)
        with reversion.create_revision():
            reversion.add_meta(RevisionMeta, source=RevisionMeta.Source.MANUAL)
            self.area.intro = "Version två"
            self.area.save()

        response = self.client.get(f"/manage/historik/areas/area/{self.area.pk}/")
        self.assertEqual(response.status_code, 200)

        oldest = list(Version.objects.get_for_object(self.area))[-1]
        self.client.post(f"/manage/historik/version/{oldest.pk}/aterstall/", {"next": "/manage/"})
        self.area.refresh_from_db()
        self.assertEqual(self.area.intro, "Gammalt intro")

    def test_unversioned_model_is_404(self):
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(f"/manage/historik/auth/user/{self.user.pk}/").status_code, 404
        )

    def test_revert_rejects_external_redirect(self):
        self.client.force_login(self.user)
        with reversion.create_revision():
            reversion.add_meta(RevisionMeta, source=RevisionMeta.Source.MANUAL)
            self.area.intro = "Version två"
            self.area.save()
        version = list(Version.objects.get_for_object(self.area))[-1]
        response = self.client.post(
            f"/manage/historik/version/{version.pk}/aterstall/",
            {"next": "https://example.com/"},
        )
        self.assertEqual(response["Location"], "/manage/")


class DiffTests(TestCase):
    def test_word_level_diff_marks_both_sides(self):
        rows = field_diffs({"intro": "snabb hjälp"}, {"intro": "snabb service"})
        self.assertEqual(len(rows), 1)
        self.assertIn("<del>hjälp</del>", rows[0]["diff"])
        self.assertIn("<ins>service</ins>", rows[0]["diff"])

    def test_unchanged_fields_are_skipped(self):
        self.assertEqual(field_diffs({"a": "x"}, {"a": "x"}), [])

    def test_html_is_escaped(self):
        rows = field_diffs({"intro": ""}, {"intro": "<script>alert(1)</script>"})
        self.assertNotIn("<script>", rows[0]["diff"])

    def test_new_object_has_no_before(self):
        rows = field_diffs(None, {"intro": "Helt ny"})
        self.assertEqual(rows[0]["old"], "")


class OperationCoverageTests(BaseCase):
    """Varje läsoperation ska svara utan att krascha på en tom-ish databas."""

    def test_read_operations_run(self):
        FAQSection.objects.create(title="Vanliga frågor")
        arguments = {
            "hamta_tjanst": {"slug": self.service.slug},
            "hamta_omrade": {"slug": self.area.slug},
            "hamta_sida": {"slug": self.page.slug},
        }
        for op in REGISTRY.values():
            if op.risk != Risk.READ:
                continue
            with self.subTest(op=op.name):
                result = op.read(self.user, **arguments.get(op.name, {}))
                self.assertIsInstance(result, dict)


class OAuthTests(BaseCase):
    """
    OAuth-flödet som Claude-appens connector använder.

    Protokollet sköts av MCP-SDK:t; det som testas här är vår del - att
    samtycket krävs, att koden är engångsbruk, och att en token faktiskt
    binder till rätt användare.
    """

    def setUp(self):
        super().setUp()
        self.oauth_client = OAuthClient.objects.create(
            client_id="test-client",
            client_name="Claude (test)",
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        )
        self.oauth_client.set_secret("hemlig")
        self.oauth_client.save()
        self.consent_url = reverse("manage:oauth_consent")
        self.query = {
            "client_id": "test-client",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": "utmaning",
            "state": "abc",
            "scopes": DEFAULT_SCOPE,
        }

    def test_client_secret_roundtrips_but_is_not_stored_in_clear(self):
        row = OAuthClient.objects.get(client_id="test-client")
        self.assertEqual(row.client_secret, "hemlig")
        self.assertNotIn(b"hemlig", bytes(row.client_secret_encrypted))

    def test_consent_requires_login(self):
        response = self.client.get(self.consent_url, self.query)
        self.assertIn(response.status_code, (302, 403))

    def test_consent_page_names_the_app(self):
        self.client.force_login(self.user)
        response = self.client.get(self.consent_url, self.query)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Claude (test)")

    def test_pkce_is_required(self):
        self.client.force_login(self.user)
        response = self.client.get(self.consent_url, {**self.query, "code_challenge": ""})
        self.assertEqual(response.status_code, 400)

    def test_redirect_uri_must_match_registration(self):
        self.client.force_login(self.user)
        response = self.client.get(
            self.consent_url, {**self.query, "redirect_uri": "https://angripare.example/cb"}
        )
        self.assertEqual(response.status_code, 400)

    def test_no_code_is_issued_without_consent(self):
        """Bara att öppna sidan får inte auktorisera något."""
        self.client.force_login(self.user)
        self.client.get(self.consent_url, self.query)
        self.assertEqual(AuthorizationCode.objects.count(), 0)

    def test_deny_returns_error_and_issues_no_code(self):
        self.client.force_login(self.user)
        response = self.client.post(
            f"{self.consent_url}?{urlencode(self.query)}", {"decision": "deny"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("error=access_denied", response["Location"])
        self.assertEqual(AuthorizationCode.objects.count(), 0)

    def test_allow_issues_a_single_use_code_bound_to_the_user(self):
        self.client.force_login(self.user)
        response = self.client.post(
            f"{self.consent_url}?{urlencode(self.query)}", {"decision": "allow"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("code=", response["Location"])
        self.assertIn("state=abc", response["Location"])

        code = AuthorizationCode.objects.get()
        self.assertEqual(code.user, self.user)
        self.assertTrue(code.is_usable)
        code.used_at = timezone.now()
        code.save(update_fields=["used_at"])
        self.assertFalse(code.is_usable)

    def test_access_token_lookup_respects_revocation_and_expiry(self):
        token, raw = OAuthToken.issue(
            OAuthToken.Kind.ACCESS, self.oauth_client, self.user, [DEFAULT_SCOPE]
        )
        self.assertEqual(OAuthToken.lookup(OAuthToken.Kind.ACCESS, raw), token)

        token.revoke()
        self.assertIsNone(OAuthToken.lookup(OAuthToken.Kind.ACCESS, raw))

        fresh, fresh_raw = OAuthToken.issue(
            OAuthToken.Kind.ACCESS, self.oauth_client, self.user, [DEFAULT_SCOPE]
        )
        fresh.expires_at = timezone.now() - timedelta(seconds=1)
        fresh.save(update_fields=["expires_at"])
        self.assertIsNone(OAuthToken.lookup(OAuthToken.Kind.ACCESS, fresh_raw))

    def test_token_kinds_do_not_cross_over(self):
        """En refresh-token får inte fungera som access-token."""
        _row, raw = OAuthToken.issue(
            OAuthToken.Kind.REFRESH, self.oauth_client, self.user, [DEFAULT_SCOPE]
        )
        self.assertIsNone(OAuthToken.lookup(OAuthToken.Kind.ACCESS, raw))

    def test_inactive_user_loses_access(self):
        _row, raw = OAuthToken.issue(
            OAuthToken.Kind.ACCESS, self.oauth_client, self.user, [DEFAULT_SCOPE]
        )
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertIsNone(OAuthToken.lookup(OAuthToken.Kind.ACCESS, raw))


class BearerHeaderTests(TestCase):
    """
    Tolerans i Authorization-headern.

    En nyckel som klistrats in via en webbläsare får ofta med sig ett hårt
    mellanslag. Symptomet blir annars en 401 utan förklaring, vilket är
    orimligt dyrt att felsöka för något som är trivialt att acceptera.
    """

    @staticmethod
    def _scope(value):
        return {"headers": [(b"authorization", value)]}

    def test_plain_bearer(self):
        self.assertEqual(_bearer_token(self._scope(b"Bearer skvvs_abc")), "skvvs_abc")

    def test_non_breaking_space_as_utf8(self):
        """Det verkliga fallet: U+00A0 går på tråden som \\xc2\\xa0."""
        self.assertEqual(_bearer_token(self._scope("Bearer  skvvs_abc".encode())), "skvvs_abc")

    def test_odd_but_harmless_whitespace(self):
        for raw in (b"bearer  skvvs_abc", b"BEARER\tskvvs_abc", b"Bearer skvvs_abc  "):
            with self.subTest(raw=raw):
                self.assertEqual(_bearer_token(self._scope(raw)), "skvvs_abc")

    def test_rejects_other_schemes_and_empty(self):
        for raw in (b"Basic skvvs_abc", b"Bearer", b"skvvs_abc", b""):
            with self.subTest(raw=raw):
                self.assertIsNone(_bearer_token(self._scope(raw)))

    def test_missing_header(self):
        self.assertIsNone(_bearer_token({"headers": []}))


class StdioCommandTests(BaseCase):
    """
    stdio-läget: Claude Desktops egen konfigurationsfil startar servern som
    en process i stället för att ansluta över nätet.
    """

    def test_requires_explicit_user_when_ambiguous(self):
        get_user_model().objects.create_superuser("nummer_tva", "t@t.local", "x")
        with self.assertRaises(CommandError):
            call_command("mcp_stdio", user=None)

    def test_unknown_user_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("mcp_stdio", user="finns-inte")

    def test_inactive_user_is_rejected(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        with self.assertRaises(CommandError):
            call_command("mcp_stdio", user=self.user.get_username())

    def test_stdio_user_reaches_the_operations(self):
        """
        Utan en HTTP-request måste användaren komma från kommandot, annars
        vägrar verktygslagret - och Claude Desktop skulle få 'saknar
        behörighet' på varje anrop.
        """
        from .mcp_server import _STDIO_USER, set_stdio_user

        self.addCleanup(set_stdio_user, _STDIO_USER)
        set_stdio_user(self.user)

        change = draft.propose(
            self.job, "uppdatera_omrade_text", {"slug": "solna", "intro": "Via stdio"}
        )
        self.assertEqual(change.job.user, self.user)
        self.area.refresh_from_db()
        self.assertEqual(self.area.intro, "Gammalt intro")


class ServiceStepTests(BaseCase):
    """
    Arbetsgången ("Så går det till") är egna rader, inte text i body.

    Utan den här operationen skrev modellen listan i body, där saneraren
    strök <h2> och <ul> och lämnade nakna textrader.
    """

    def test_steps_become_rows_not_body_text(self):
        change = draft.propose(
            self.job,
            "satt_tjanst_steg",
            {
                "slug": self.service.slug,
                "steg": [
                    {"rubrik": "Vi stänger av vattnet"},
                    {
                        "rubrik": "Gammal blandare demonteras",
                        "beskrivning": "Kopplingar kontrolleras.",
                    },
                ],
            },
        )
        self.assertEqual(self.service.steps.count(), 0)  # förslag skriver inget

        draft.approve(change, self.user)
        steps = list(self.service.steps.all())
        self.assertEqual(
            [s.title for s in steps], ["Vi stänger av vattnet", "Gammal blandare demonteras"]
        )
        self.assertEqual([s.order for s in steps], [0, 1])
        self.service.refresh_from_db()
        self.assertNotIn("stänger av vattnet", self.service.body or "")

    def test_steps_replace_rather_than_append(self):
        ServiceStep.objects.create(service=self.service, title="Gammalt steg", order=0)
        change = draft.propose(
            self.job,
            "satt_tjanst_steg",
            {"slug": self.service.slug, "steg": [{"rubrik": "Enda steget"}]},
        )
        draft.approve(change, self.user)
        self.assertEqual([s.title for s in self.service.steps.all()], ["Enda steget"])

    def test_steps_are_sanitised(self):
        change = draft.propose(
            self.job,
            "satt_tjanst_steg",
            {"slug": self.service.slug, "steg": [{"rubrik": "Vi spolar — noggrant"}]},
        )
        self.assertNotIn("—", change.payload["steg"][0]["title"])

    def test_rejects_empty_and_oversized_lists(self):
        for steg in ([], [{"beskrivning": "utan rubrik"}], [{"rubrik": "x"}] * 13):
            with self.subTest(steg=steg):
                with self.assertRaises(OperationError):
                    draft.propose(
                        self.job,
                        "satt_tjanst_steg",
                        {"slug": self.service.slug, "steg": steg},
                    )

    def test_body_description_warns_about_stripped_markup(self):
        """
        Modellen ser aldrig resultatet av saneringen, så varningen måste stå
        i verktygsbeskrivningen - annars upprepas felet.
        """
        for name in ("uppdatera_tjanst_text", "uppdatera_omrade_text", "skapa_faq_fraga"):
            with self.subTest(op=name):
                self.assertIn("stryks", REGISTRY[name].description.lower())


class ReviewGroupingTests(BaseCase):
    """Granskningsvyn grupperar per objekt, inte per riskklass."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.other = Service.objects.create(name="Avloppsspolning", category=self.category)

    def test_changes_group_by_target_object(self):
        draft.propose(
            self.job, "uppdatera_tjanst_text", {"slug": self.service.slug, "description": "A"}
        )
        draft.propose(
            self.job, "satt_tjanst_steg", {"slug": self.service.slug, "steg": [{"rubrik": "Ett"}]}
        )
        draft.propose(
            self.job, "uppdatera_tjanst_text", {"slug": self.other.slug, "description": "B"}
        )

        from .views import _change_rows

        groups, _decided = _change_rows(self.job)
        self.assertEqual(len(groups), 2)
        by_label = {g["label"]: g for g in groups}
        self.assertEqual(len(by_label[self.service.name]["rows"]), 2)
        self.assertEqual(len(by_label[self.other.name]["rows"]), 1)

    def test_checkboxes_are_not_preselected(self):
        """
        Ett granskningsverktyg vars snabbaste klick godkänner allt är inget
        granskningsverktyg.
        """
        draft.propose(
            self.job, "uppdatera_tjanst_text", {"slug": self.service.slug, "description": "A"}
        )
        html = self.client.get(f"/manage/ai/jobb/{self.job.pk}/").content.decode()
        self.assertIn('name="change_ids"', html)
        # Leta i MARKUPEN, inte i hela svaret: ordet "checked" finns numera
        # också i skriptet som räknar markeringar.
        import re

        inputs = re.findall(r"<input\b[^>]*>", html)
        self.assertTrue(inputs)
        self.assertFalse(
            [tag for tag in inputs if re.search(r"\bchecked\b", tag)],
            "Ingen kryssruta får vara förvald.",
        )

    def test_business_changes_are_selectable_but_marked(self):
        """Markering tillåten, men märkningen ska synas i granskningen."""
        change = draft.propose(
            self.job, "satt_tjanst_aktiv", {"slug": self.service.slug, "aktiv": False}
        )
        html = self.client.get(f"/manage/ai/jobb/{self.job.pk}/").content.decode()
        self.assertIn(f'value="{change.pk}"', html)
        self.assertIn('data-business="1"', html)
        self.assertIn("Affärsdata", html)

    def test_group_form_only_approves_its_own_group(self):
        mine = draft.propose(
            self.job, "uppdatera_tjanst_text", {"slug": self.service.slug, "description": "A"}
        )
        theirs = draft.propose(
            self.job, "uppdatera_tjanst_text", {"slug": self.other.slug, "description": "B"}
        )

        self.client.post(
            f"/manage/ai/jobb/{self.job.pk}/bulk/", {"change_ids": [mine.pk], "action": "approve"}
        )
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.status, DraftChange.Status.APPLIED)
        self.assertEqual(theirs.status, DraftChange.Status.PENDING)


class SilentLossTests(BaseCase):
    """
    Saneringen får inte äta upp innehåll utan att modellen får veta.

    Det var så "Så går det till" hamnade som naken text i brödtexten:
    modellen skickade <h2> och <ul>, saneringen strök dem, utkastet
    skapades ändå, och modellen fick ett kvitto som såg lyckat ut. En
    varning i verktygsbeskrivningen räcker inte - den förlitar sig på att
    modellen läser och lyder. Det här stoppar det i stället.
    """

    #: Exakt vad modellen skickade när felet uppstod.
    ORIGINAL = (
        "<p>En blandare som droppar behöver bytas.</p>"
        "<h2>Så går det till</h2>"
        "<ul><li>Vi stänger av vattnet.</li></ul>"
    )

    def test_the_original_bug_is_now_rejected(self):
        with self.assertRaises(OperationError) as ctx:
            draft.propose(
                self.job,
                "uppdatera_tjanst_text",
                {"slug": self.service.slug, "body": self.ORIGINAL},
            )
        message = str(ctx.exception)
        self.assertIn("h2", message)
        self.assertIn("ul", message)
        self.assertIn("satt_tjanst_steg", message)

    def test_area_body_is_protected_too(self):
        with self.assertRaises(OperationError):
            draft.propose(
                self.job,
                "uppdatera_omrade_text",
                {"slug": self.area.slug, "body": self.ORIGINAL},
            )

    def test_allowed_markup_still_passes(self):
        change = draft.propose(
            self.job,
            "uppdatera_tjanst_text",
            {
                "slug": self.service.slug,
                "body": "<p>Vi byter <strong>blandare</strong> och "
                '<em>packningar</em>. <a href="/kontakt/">Kontakt</a>.</p>',
            },
        )
        self.assertIn("<strong>", change.payload["body"])
        self.assertIn("<a", change.payload["body"])

    def test_plain_text_passes(self):
        draft.propose(
            self.job,
            "uppdatera_tjanst_text",
            {"slug": self.service.slug, "description": "Helt vanlig text."},
        )

    def test_markup_in_steps_is_rejected(self):
        with self.assertRaises(OperationError):
            draft.propose(
                self.job,
                "satt_tjanst_steg",
                {"slug": self.service.slug, "steg": [{"rubrik": "<h3>Vi stänger av vattnet</h3>"}]},
            )

    def test_markup_in_blocks_is_rejected(self):
        from apps.website.models import Block

        block = Block.objects.create(page=self.page, block_type="article", data={}, order=1)
        with self.assertRaises(OperationError):
            draft.propose(
                self.job,
                "uppdatera_block",
                {"block_id": block.pk, "falt": {"body": self.ORIGINAL}},
            )

    def test_every_write_operation_rejects_stripped_markup(self):
        """
        Regressionsspärr, körd som beteende och inte källkodsläsning: varje
        skrivoperation som tar fritext matas med markup som inte överlever,
        och måste vägra. En ny operation som glömmer skyddet faller här.
        """
        from apps.website.models import Block

        block = Block.objects.create(page=self.page, block_type="article", data={}, order=1)
        section = FAQSection.objects.create(title="Vanliga frågor")
        item = section.items.create(question="Fråga?", answer="Svar.")

        poisoned = {
            "uppdatera_block": {"block_id": block.pk, "falt": {"body": self.ORIGINAL}},
            "skapa_block": {
                "sid_slug": self.page.slug,
                "blocktyp": "article",
                "falt": {"body": self.ORIGINAL},
            },
            "uppdatera_sidmeta": {"slug": self.page.slug, "meta_description": self.ORIGINAL},
            "skapa_sida": {"titel": "Ny", "meta_description": self.ORIGINAL},
            "uppdatera_tjanst_text": {"slug": self.service.slug, "body": self.ORIGINAL},
            "skapa_tjanst": {
                "namn": "Ny tjänst",
                "beskrivning": "Kort.",
                "steg": [{"rubrik": "Vi kommer"}],
                "body": self.ORIGINAL,
            },
            "satt_tjanst_steg": {"slug": self.service.slug, "steg": [{"rubrik": "<h3>Steg</h3>"}]},
            "uppdatera_omrade_text": {"slug": self.area.slug, "body": self.ORIGINAL},
            "skapa_omrade": {
                "namn": "Nyköping",
                "niva": "kommun",
                "overordnad_slug": self.region.slug,
                "body": self.ORIGINAL,
            },
            "skapa_faq_fraga": {"sektion_slug": section.slug, "fraga": "F?", "svar": self.ORIGINAL},
            "uppdatera_faq_fraga": {"fraga_id": item.pk, "svar": self.ORIGINAL},
            "skapa_faq_sektion": {"titel": "Ny sektion", "beskrivning": self.ORIGINAL},
            "uppdatera_faq_sektion": {
                "sektion_slug": section.slug,
                "beskrivning": self.ORIGINAL,
            },
        }

        # Tabellen måste täcka alla skrivoperationer som tar fritext, annars
        # kan en ny operation smita förbi genom att bara saknas här.
        text_keys = {
            "body",
            "intro",
            "svar",
            "falt",
            "steg",
            "beskrivning",
            "description",
            "meta_description",
            "rubrik",
        }
        expected = {
            op.name
            for op in REGISTRY.values()
            if op.risk != Risk.READ and text_keys & set(op.input_schema.get("properties", {}))
        }
        self.assertEqual(expected - set(poisoned), set(), "Nya operationer saknas i tabellen")

        for name, params in poisoned.items():
            with self.subTest(op=name):
                with self.assertRaises(OperationError):
                    draft.propose(self.job, name, params)


class StaleDraftTests(BaseCase):
    """
    Ett utkast får inte tyst skriva över en redigering som gjorts under tiden.

    `before` sparades från början men jämfördes aldrig - så AI:ns förslag
    vann alltid, även om kunden hunnit ändra samma fält själv.
    """

    def test_manual_edit_blocks_silent_overwrite(self):
        change = draft.propose(
            self.job,
            "uppdatera_tjanst_text",
            {"slug": self.service.slug, "description": "AI:ns förslag"},
        )
        self.service.description = "Kundens egen redigering"
        self.service.save(update_fields=["description"])

        with self.assertRaises(OperationError) as ctx:
            draft.approve(change, self.user)
        self.assertIn("ändrats sedan förslaget", str(ctx.exception))

        self.service.refresh_from_db()
        self.assertEqual(self.service.description, "Kundens egen redigering")

    def test_force_lets_the_customer_overwrite_deliberately(self):
        change = draft.propose(
            self.job,
            "uppdatera_tjanst_text",
            {"slug": self.service.slug, "description": "AI:ns förslag"},
        )
        self.service.description = "Kundens egen redigering"
        self.service.save(update_fields=["description"])

        draft.approve(change, self.user, force=True)
        self.service.refresh_from_db()
        self.assertEqual(self.service.description, "AI:ns förslag")

    def test_untouched_draft_applies_without_force(self):
        change = draft.propose(
            self.job,
            "uppdatera_tjanst_text",
            {"slug": self.service.slug, "description": "Ny text"},
        )
        draft.approve(change, self.user)
        self.service.refresh_from_db()
        self.assertEqual(self.service.description, "Ny text")

    def test_new_objects_are_never_stale(self):
        change = draft.propose(
            self.job,
            "skapa_tjanst",
            {"namn": "Avloppsspolning", "beskrivning": "Kort.", "steg": [{"rubrik": "Vi kommer"}]},
        )
        self.assertEqual(draft.stale_fields(change, None), set())
        draft.approve(change, self.user)


class ChatTests(BaseCase):
    """
    Den inbyggda chatten. Modellen mockas - det som testas är att slingan
    respekterar samma gränser som MCP-vägen.
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    @staticmethod
    def _text_response(text):
        block = SimpleNamespace(type="text", text=text)
        return SimpleNamespace(
            content=[block],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0),
        )

    @staticmethod
    def _tool_response(name, params):
        block = SimpleNamespace(type="tool_use", id="t1", name=name, input=params)
        return SimpleNamespace(
            content=[block],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0),
        )

    def test_a_write_through_chat_becomes_a_draft_not_a_write(self):
        """Kärnan: chatten får inte vara en genväg förbi godkännandet."""
        from . import chat as engine

        job, reply = engine.start_turn(self.user, "Skriv om beskrivningen")
        responses = [
            self._tool_response(
                "uppdatera_tjanst_text", {"slug": self.service.slug, "description": "Nytt"}
            ),
            self._text_response("Klart, ett utkast ligger för granskning."),
        ]
        with patch("apps.assistant.chat.call", side_effect=responses):
            engine.run_turn(reply.pk)

        reply.refresh_from_db()
        self.assertEqual(reply.status, ChatMessage.Status.DONE)
        self.assertEqual(job.changes.filter(status=DraftChange.Status.PENDING).count(), 1)
        self.service.refresh_from_db()
        self.assertEqual(self.service.description, "Gammal text")

    def test_tool_errors_go_back_to_the_model_not_the_user(self):
        from . import chat as engine

        _job, reply = engine.start_turn(self.user, "Ändra en tjänst som inte finns")
        responses = [
            self._tool_response(
                "uppdatera_tjanst_text", {"slug": "finns-inte", "description": "x"}
            ),
            self._text_response("Den tjänsten fanns inte."),
        ]
        with patch("apps.assistant.chat.call", side_effect=responses):
            engine.run_turn(reply.pk)

        reply.refresh_from_db()
        self.assertEqual(reply.status, ChatMessage.Status.DONE)
        self.assertFalse(reply.steps[0]["ok"])

    def test_budget_ceiling_stops_the_turn(self):
        from . import chat as engine
        from .llm import BudgetExceeded

        _job, reply = engine.start_turn(self.user, "Gör något dyrt")
        with patch("apps.assistant.chat.call", side_effect=BudgetExceeded("Budget slut.")):
            engine.run_turn(reply.pk)

        reply.refresh_from_db()
        self.assertEqual(reply.status, ChatMessage.Status.FAILED)
        self.assertIn("Budget", reply.error)

    def test_runaway_loop_is_braked(self):
        """En modell som aldrig slutar anropa verktyg måste stoppas av oss."""
        from . import chat as engine

        _job, reply = engine.start_turn(self.user, "Loopa")
        forever = self._tool_response("lista_tjanster", {})
        with patch("apps.assistant.chat.call", return_value=forever) as mocked:
            engine.run_turn(reply.pk)
        self.assertLessEqual(mocked.call_count, engine.MAX_TOOL_CALLS + 2)

    def test_empty_question_is_rejected(self):
        from . import chat as engine

        with self.assertRaises(OperationError):
            engine.start_turn(self.user, "   ")

    def test_chat_page_and_poll(self):
        from . import chat as engine

        job, reply = engine.start_turn(self.user, "Hej")
        self.assertEqual(self.client.get("/manage/ai/").status_code, 200)
        self.assertEqual(self.client.get(f"/manage/ai/samtal/{job.pk}/").status_code, 200)

        poll = self.client.get(f"/manage/ai/samtal/{job.pk}/status/").json()
        self.assertEqual(poll["status"], "pending")

        reply.status = ChatMessage.Status.DONE
        reply.content = "Svar"
        reply.save()
        self.assertEqual(
            self.client.get(f"/manage/ai/samtal/{job.pk}/status/").json()["status"], "done"
        )

    def test_another_users_conversation_is_not_reachable(self):
        other = get_user_model().objects.create_user("annan", "a@t.local", "x")
        job = AIJob.objects.create(user=other, title="Andras")
        self.assertEqual(self.client.get(f"/manage/ai/samtal/{job.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/manage/ai/samtal/{job.pk}/status/").status_code, 404)


class CostTests(BaseCase):
    def test_cost_is_recorded_per_call(self):
        from .llm import cost_micros

        # 1M in + 1M ut på opus-5 = 5 + 25 USD
        self.assertEqual(cost_micros("claude-opus-5", 1_000_000, 1_000_000), 30_000_000)

    def test_cached_tokens_cost_a_tenth(self):
        from .llm import cost_micros

        full = cost_micros("claude-opus-5", 1_000_000, 0)
        cached = cost_micros("claude-opus-5", 0, 0, cached_in=1_000_000)
        self.assertAlmostEqual(cached, full * 0.1, delta=1)

    def test_budget_guard_trips_at_the_ceiling(self):
        from django.test import override_settings

        from .llm import BudgetExceeded, check_budget
        from .models import AICall

        with override_settings(ASSISTANT_DAILY_BUDGET_USD=1.0):
            check_budget()  # inget spenderat ännu
            AICall.objects.create(user=self.user, model="claude-opus-5", cost_micros=1_200_000)
            with self.assertRaises(BudgetExceeded):
                check_budget()


class BedrockTests(BaseCase):
    """
    Modellen går via Bedrock med instansrollen. Ingen nyckel ska behövas,
    och de fel som faktiskt inträffar ska gå att agera på.
    """

    def test_bedrock_is_the_default_provider(self):
        from .llm import is_configured, model_id, provider

        self.assertEqual(provider(), "bedrock")
        self.assertTrue(model_id().startswith("eu.anthropic."))
        # Instansrollen avgör - inget att konfigurera i appen.
        self.assertTrue(is_configured())

    def test_bedrock_client_needs_no_api_key(self):
        from django.test import override_settings

        from .llm import client

        with override_settings(ANTHROPIC_API_KEY=""):
            with patch("anthropic.AnthropicBedrock") as mocked:
                client()
                self.assertEqual(mocked.call_args.kwargs["aws_region"], "eu-central-1")

    def test_direct_provider_still_requires_a_key(self):
        from django.test import override_settings

        from .llm import ModelUnavailable, client, is_configured

        with override_settings(ASSISTANT_PROVIDER="anthropic", ANTHROPIC_API_KEY=""):
            self.assertFalse(is_configured())
            with self.assertRaises(ModelUnavailable):
                client()

    def test_bedrock_prices_differ_from_first_party(self):
        from .llm import cost_micros

        bedrock = cost_micros("eu.anthropic.claude-sonnet-4-6", 1_000_000, 0)
        direct = cost_micros("claude-sonnet-5", 1_000_000, 0)
        self.assertEqual(bedrock, 3_500_000)
        self.assertEqual(direct, 3_000_000)

    def test_unknown_model_is_priced_high_not_free(self):
        """En felmätning uppåt syns; en nedåt gör mätaren till en lögn."""
        from .llm import DEFAULT_PRICE, cost_micros

        self.assertEqual(
            cost_micros("eu.anthropic.nagot-nytt", 1_000_000, 0),
            round(DEFAULT_PRICE[0] * 1_000_000),
        )

    def test_marketplace_denial_gets_an_actionable_message(self):
        """
        Det här felet bet Atlas-projektet 2026-07-08: utan
        aws-marketplace-rättigheterna nekas FÖRSTA anropet mot en ny modell,
        och AWS felmeddelande nämner inte IAM.
        """
        from .llm import _friendly

        text = _friendly(Exception("Model access is denied for aws-marketplace"))
        self.assertIn("aws-marketplace:Subscribe", text)
        self.assertIn("bedrock:InvokeModel", text)

    def test_missing_credentials_gets_an_actionable_message(self):
        from .llm import _friendly

        self.assertIn("~/.aws", _friendly(Exception("NoCredentialsError")))

    def test_a_failed_call_is_still_metered(self):
        from .llm import ModelUnavailable, call
        from .models import AICall

        with patch("apps.assistant.llm.client", side_effect=RuntimeError("nere")):
            with self.assertRaises(ModelUnavailable):
                call(system="s", messages=[], tools=[], user=self.user)

        row = AICall.objects.latest("created_at")
        self.assertFalse(row.ok)
        self.assertIn("nere", row.error)


class ModelPolicyTests(TestCase):
    """
    Kundens regel 2026-08-21: bara Sonnet/Opus från 4.6, aldrig Haiku.

    Testet finns för att regeln är en affärsregel, inte en teknisk detalj.
    Den syns inte i något gränssnitt och skulle annars kunna falla bort vid
    en modelluppgradering utan att någon märker det förrän texten är sämre.
    """

    def test_allows_sonnet_and_opus_from_4_6(self):
        for model in (
            "eu.anthropic.claude-sonnet-4-6",
            "eu.anthropic.claude-opus-4-6-v1",
            "eu.anthropic.claude-opus-4-8",
            "eu.anthropic.claude-opus-5",
            "claude-opus-5",
        ):
            with self.subTest(model=model):
                llm.assert_model_allowed(model)

    def test_rejects_haiku_regardless_of_version(self):
        for model in (
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "eu.anthropic.claude-3-haiku-20240307-v1:0",
        ):
            with self.subTest(model=model):
                with self.assertRaises(llm.ModelUnavailable):
                    llm.assert_model_allowed(model)

    def test_rejects_versions_below_4_6(self):
        for model in (
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "eu.anthropic.claude-opus-4-5-20251101-v1:0",
            "eu.anthropic.claude-sonnet-4-20250514-v1:0",
        ):
            with self.subTest(model=model):
                with self.assertRaises(llm.ModelUnavailable):
                    llm.assert_model_allowed(model)

    def test_datestamp_is_not_read_as_version(self):
        """claude-3-sonnet-20240229 får inte tolkas som version 20240229."""
        with self.assertRaises(llm.ModelUnavailable):
            llm.assert_model_allowed("eu.anthropic.claude-3-sonnet-20240229-v1:0")

    def test_rejects_unknown_family(self):
        with self.assertRaises(llm.ModelUnavailable):
            llm.assert_model_allowed("global.anthropic.claude-fable-5")

    def test_configured_default_passes_its_own_rule(self):
        """Defaulten i settings ska aldrig vara en modell spärren nekar."""
        llm.assert_model_allowed(llm.model_id())


class StepLabelTests(TestCase):
    """Chatten visar etiketter, inte funktionsnamn."""

    def test_every_operation_has_a_label(self):
        """
        Ny operation utan etikett visas som rå kod i chatten. Testet gör
        det till ett rött test i stället för en kosmetisk miss ingen ser.
        """
        from apps.assistant.operations import all_operations
        from apps.assistant.runtime import STEP_LABELS

        missing = sorted(o.name for o in all_operations() if o.name not in STEP_LABELS)
        self.assertEqual(missing, [], f"Saknar etikett i STEP_LABELS: {missing}")

    def test_unknown_operation_falls_back_to_its_name(self):
        from apps.assistant.runtime import step_label

        self.assertEqual(step_label("nagot_nytt"), "nagot_nytt")


class AISectionNavTests(TestCase):
    """
    Undermenyn ska finnas på ALLA sidor i AI-sektionen.

    Den låg tidigare som knappar i chattsidans rubrik och försvann så fort
    man klickade vidare - man hamnade i en återvändsgränd och fick gå via
    toppmenyn tillbaka.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("navuser", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="Nav")

    def test_subnav_on_every_ai_page(self):
        for url in (
            reverse("manage:assistant_chat"),
            reverse("manage:assistant_jobs"),
            reverse("manage:assistant_job", args=[self.job.pk]),
            reverse("manage:assistant_connection"),
        ):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertIn('class="m-subnav"', html)
                for target in ("assistant_chat", "assistant_jobs", "assistant_connection"):
                    self.assertIn(reverse(f"manage:{target}"), html)


class ReleaseGatingTests(TestCase):
    """
    AI-delen släpptes till kunden 2026-08-22: chatt, förslag och anslutning
    är öppna för inloggade. Det enda som förblir byråns är personliga
    API-nycklar - kunden ansluter via inloggning och samtycke, aldrig kod.
    """

    def setUp(self):
        self.customer = get_user_model().objects.create_user("kundx", password="x", is_staff=True)
        self.client.force_login(self.customer)

    def test_customer_reaches_the_whole_ai_section(self):
        for name in ("assistant_chat", "assistant_jobs", "assistant_connection"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"manage:{name}")).status_code, 200)

    def test_ai_visible_in_the_nav(self):
        html = self.client.get(reverse("manage:dashboard")).content.decode()
        self.assertIn(reverse("manage:assistant_chat"), html)

    def test_key_management_stays_superuser_only(self):
        response = self.client.post(reverse("manage:assistant_token_create"))
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(AssistantToken.objects.filter(user=self.customer).exists())

    def test_customer_sees_no_key_sections_on_the_connection_page(self):
        html = self.client.get(reverse("manage:assistant_connection")).content.decode()
        self.assertNotIn("Claude Code", html)
        self.assertNotIn("Skapa nyckel", html)
        self.assertNotIn("personliga nycklar", html.lower())

    def test_superuser_still_sees_key_sections(self):
        boss = get_user_model().objects.create_superuser("chef9", password="x")
        self.client.force_login(boss)
        html = self.client.get(reverse("manage:assistant_connection")).content.decode()
        self.assertIn("Claude Code", html)
        self.assertIn("Skapa nyckel", html)


class MentionTests(TestCase):
    """@-tokens: sökning, kontext till modellen och chips i loggen."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("mention", password="x")
        self.client.force_login(self.user)
        category = ServiceCategory.objects.create(name="Avlopp")
        self.service = Service.objects.create(
            category=category, name="Spolning", body="text", is_active=True
        )

    def test_search_finds_service(self):
        response = self.client.get(reverse("manage:assistant_mention_search"), {"q": "spol"})
        results = response.json()["results"]
        self.assertTrue(
            any(r["typ"] == "tjanst" and r["ref"] == self.service.slug for r in results)
        )

    def test_context_resolves_token_to_exact_object(self):
        from apps.assistant import mentions

        ctx = mentions.context_for(f"Uppdatera @tjanst:{self.service.slug} tack", self.user)
        self.assertIn("Spolning", ctx)
        self.assertIn("hamta_tjanst", ctx)

    def test_unknown_token_gives_no_context(self):
        """Modellen ska aldrig få påhittade referenser."""
        from apps.assistant import mentions

        self.assertEqual(mentions.context_for("@tjanst:finns-inte", self.user), "")

    def test_context_is_not_stored_in_the_message(self):
        """Referensblocket läggs på vid anropet - kundens text förblir kundens."""
        import inspect

        from apps.assistant import chat

        source = inspect.getsource(chat.run_turn)
        self.assertIn("context_for", source)

    def test_as_html_escapes_but_renders_chip(self):
        from apps.assistant import mentions

        html = mentions.as_html(f"<script>x</script> @tjanst:{self.service.slug}")
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('class="m-chip"', html)

    def test_other_users_jobs_are_not_searchable(self):
        other = get_user_model().objects.create_user("annan", password="x")
        AIJob.objects.create(user=other, title="Hemligt förslag")
        response = self.client.get(reverse("manage:assistant_mention_search"), {"q": "Hemligt"})
        self.assertEqual(response.json()["results"], [])


class FollowupTests(TestCase):
    """Underförslag: nästa steg för objekt som fått något godkänt."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("followup", password="x")
        self.client.force_login(self.user)
        category = ServiceCategory.objects.create(name="Avlopp")
        self.service = Service.objects.create(
            category=category, name="Stambyte", body="", is_active=True
        )

    def test_service_without_steps_and_faq_gets_both(self):
        from apps.assistant import suggestions

        titles = [f["title"] for f in suggestions.followups_for(self.service)]
        self.assertIn("Skriv arbetsgången", titles)
        self.assertIn("Skapa FAQ för tjänsten", titles)

    def test_prompt_carries_a_mention_token(self):
        from apps.assistant import suggestions

        prompts = " ".join(f["prompt"] for f in suggestions.followups_for(self.service))
        self.assertIn(f"@tjanst:{self.service.slug}", prompts)

    def test_followups_shown_after_approval(self):
        from django.contrib.contenttypes.models import ContentType

        job = AIJob.objects.create(user=self.user, title="Test")
        DraftChange.objects.create(
            job=job,
            operation="uppdatera_tjanst_text",
            risk=Risk.TEXT,
            status=DraftChange.Status.APPLIED,
            summary="x",
            payload={},
            target_ct=ContentType.objects.get_for_model(Service),
            target_id=self.service.pk,
        )
        html = self.client.get(reverse("manage:assistant_job", args=[job.pk])).content.decode()
        self.assertIn("Nästa steg", html)
        self.assertIn("Skriv arbetsgången", html)

    def test_prefill_lands_in_the_textarea(self):
        html = self.client.get(
            reverse("manage:assistant_chat"), {"q": "Skriv arbetsgången för @tjanst:stambyte"}
        ).content.decode()
        self.assertIn("Skriv arbetsgången för @tjanst:stambyte", html)


class MentionBrowseTests(TestCase):
    """Ren @ ska visa en bläddringslista - en tom meny läses som trasig."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("browse", password="x")
        self.client.force_login(self.user)

    def test_empty_query_returns_a_browse_list(self):
        from apps.assistant import mentions

        category = ServiceCategory.objects.create(name="Avlopp")
        Service.objects.create(category=category, name="Spolning", is_active=True)
        results = mentions.search("", self.user)
        self.assertTrue(results)
        self.assertIn("tjanst", {r["typ"] for r in results})

    def test_all_types_are_searchable(self):
        from apps.assistant import mentions
        from apps.faq.models import FAQItem, FAQSection

        ServiceCategory.objects.create(name="Avlopp")
        section = FAQSection.objects.create(title="Vanliga frågor", slug="vanliga")
        FAQItem.objects.create(section=section, question="Vad kostar en spolning?")

        types = {r["typ"] for r in mentions.search("a", self.user, limit=30)}
        for expected in ("kategori", "faq", "fraga"):
            self.assertIn(expected, types)

    def test_new_tokens_resolve_to_context(self):
        from apps.assistant import mentions
        from apps.faq.models import FAQItem, FAQSection

        section = FAQSection.objects.create(title="Vanliga frågor", slug="vanliga")
        item = FAQItem.objects.create(section=section, question="Vad kostar det?")
        ctx = mentions.context_for(f"Uppdatera @fraga:{item.pk}", self.user)
        self.assertIn("Vad kostar det?", ctx)
        self.assertIn("fraga_id", ctx)


class JobDeleteTests(TestCase):
    """Radering av förslag och samtal."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("delete", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="Att radera")

    def test_delete_removes_job_and_its_drafts(self):
        DraftChange.objects.create(
            job=self.job,
            operation="uppdatera_tjanst_text",
            risk=Risk.TEXT,
            status=DraftChange.Status.PENDING,
            summary="x",
            payload={},
        )
        self.client.post(reverse("manage:assistant_job_delete", args=[self.job.pk]))
        self.assertFalse(AIJob.objects.filter(pk=self.job.pk).exists())
        self.assertFalse(DraftChange.objects.filter(job_id=self.job.pk).exists())

    def test_version_history_survives_deletion(self):
        """
        Genomförda ändringar ska finnas kvar under /manage/historik/.

        RevisionMeta.job är SET_NULL just för det - raderar man ett förslag
        får man inte tappa spåret av vad som faktiskt publicerats.
        """
        with reversion.create_revision():
            section = FAQSection.objects.create(title="Kvar", slug="kvar")
        revision = Version.objects.get_for_object(section).first().revision
        RevisionMeta.objects.create(revision=revision, source=RevisionMeta.Source.AI, job=self.job)
        self.client.post(reverse("manage:assistant_job_delete", args=[self.job.pk]))
        meta = RevisionMeta.objects.get(revision=revision)
        self.assertIsNone(meta.job_id)
        self.assertEqual(meta.source, RevisionMeta.Source.AI)

    def test_cannot_delete_another_users_job(self):
        other = get_user_model().objects.create_user("annan2", password="x")
        theirs = AIJob.objects.create(user=other, title="Deras")
        response = self.client.post(reverse("manage:assistant_job_delete", args=[theirs.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(AIJob.objects.filter(pk=theirs.pk).exists())

    def test_get_does_not_delete(self):
        response = self.client.get(reverse("manage:assistant_job_delete", args=[self.job.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(AIJob.objects.filter(pk=self.job.pk).exists())

    def test_delete_button_in_the_draft_list(self):
        html = self.client.get(reverse("manage:assistant_jobs")).content.decode()
        self.assertIn(reverse("manage:assistant_job_delete", args=[self.job.pk]), html)

    def test_delete_button_in_the_chat_sidebar(self):
        """Sidolistan visar bara jobb som HAR meddelanden."""
        ChatMessage.objects.create(job=self.job, role=ChatRole.USER, status="done", content="hej")
        html = self.client.get(reverse("manage:assistant_chat")).content.decode()
        self.assertIn(reverse("manage:assistant_job_delete", args=[self.job.pk]), html)


class ChatChromeTests(TestCase):
    """Det som togs bort 2026-08-21 ska förbli borta."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("chrome", password="x")
        self.client.force_login(self.user)

    def test_no_cost_panel_even_for_superuser(self):
        html = self.client.get(reverse("manage:assistant_chat")).content.decode()
        self.assertNotIn("Kostnad idag", html)
        self.assertNotIn("USD", html)

    def test_no_example_prompts_above_the_chat(self):
        html = self.client.get(reverse("manage:assistant_chat")).content.decode()
        self.assertNotIn("m-example", html)


class EmptyStateTests(TestCase):
    """Tomma lägen ska ha padding och kort text."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("empty", password="x")
        self.client.force_login(self.user)

    def test_empty_draft_list(self):
        html = self.client.get(reverse("manage:assistant_jobs")).content.decode()
        self.assertIn("Inga förslag hittades.", html)
        # .m-panel saknar padding med flit (tabeller fyller den); textpaneler
        # måste därför bära m-empty, annars hamnar texten i kant.
        self.assertIn("m-panel m-empty", html)
        self.assertNotIn("Koppla din Claude-", html)


class FeatureGateTests(TestCase):
    """Moduler kunden inte betalat för ska varken synas eller gå att anropa."""

    def test_stats_is_off_by_default(self):
        from apps.assistant.runtime import tool_descriptions

        names = [t.get("name") if isinstance(t, dict) else t[0] for t in tool_descriptions()]
        self.assertNotIn("hamta_statistik", names)

    def test_calling_a_disabled_tool_is_refused(self):
        """
        Filtrering i verktygslistan är ingen behörighetskontroll - en
        MCP-klient kan anropa vilket namn som helst.
        """
        from apps.assistant.runtime import run_operation

        with self.assertRaises(OperationError):
            run_operation(self.user, lambda: None, "hamta_statistik", {})

    def test_enabling_the_module_brings_it_back(self):
        from apps.assistant.runtime import tool_descriptions

        with self.settings(ASSISTANT_FEATURES={"statistik": True}):
            names = [t.get("name") if isinstance(t, dict) else t[0] for t in tool_descriptions()]
            self.assertIn("hamta_statistik", names)

    def test_operations_without_a_feature_are_always_available(self):
        from apps.assistant.runtime import available_operations

        names = {op.name for op in available_operations()}
        self.assertIn("lista_tjanster", names)
        self.assertIn("hamta_skrivguide", names)

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("feature", password="x")


class MarkdownRenderingTests(TestCase):
    """Assistentens svar är markdown och ska renderas - säkert."""

    def test_renders_lists_and_emphasis(self):
        from apps.assistant.rendering import message_html

        html = message_html("**fet**\n\n- ett\n- två")
        self.assertIn("<strong>fet</strong>", html)
        self.assertIn("<li>ett</li>", html)

    def test_strips_script_and_image(self):
        """
        Modellens text är inte betrodd - den kan bära text som en webbsida
        matat in via ett läsverktyg.
        """
        from apps.assistant.rendering import message_html

        html = message_html("<script>alert(1)</script><img src=x onerror=alert(1)>")
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("onerror", html)

    def test_headings_become_bold_not_h1(self):
        """Rubriker i en bubbla skulle konkurrera med sidans rubriknivåer."""
        from apps.assistant.rendering import message_html

        html = message_html("## Nästa steg")
        self.assertNotIn("<h2", html)
        self.assertIn("<strong>Nästa steg</strong>", html)

    def test_links_get_noopener(self):
        from apps.assistant.rendering import message_html

        html = message_html("[x](http://example.com/)")
        self.assertIn('rel="noopener noreferrer"', html)

    def test_mention_tokens_still_become_chips(self):
        from apps.assistant.rendering import message_html

        self.assertIn('class="m-chip"', message_html("Se @tjanst:spolning"))

    def test_user_text_is_not_rendered_as_markdown(self):
        """Kundens egen text ska visas som skriven, inte tolkas."""
        from apps.assistant import mentions

        self.assertNotIn("<strong>", mentions.as_html("**inte fet**"))


class ProgressFeedbackTests(TestCase):
    """Riktig återkoppling medan turen kör, inte bara 'Arbetar'."""

    def test_running_state_is_saved_before_the_tool_runs(self):
        """
        Steget måste sparas som pågående INNAN verktyget kör - annars står
        gränssnittet stilla under hela anropet.
        """
        import inspect

        from apps.assistant import chat

        source = inspect.getsource(chat.run_turn)
        running = source.index('"state": "running"')
        call = source.index("run_operation(user")
        self.assertLess(running, call)

    def test_narration_is_captured(self):
        """Modellens text mellan verktygsanropen är den bästa feedbacken."""
        import inspect

        from apps.assistant import chat

        self.assertIn('steps.append({"note"', inspect.getsource(chat.run_turn))

    def test_arg_hint_identifies_the_object(self):
        from apps.assistant.chat import _arg_hint

        self.assertEqual(_arg_hint({"slug": "byte-av-blandare"}), "byte-av-blandare")
        self.assertEqual(_arg_hint({"titel": "Vanliga frågor"}), "Vanliga frågor")
        self.assertEqual(_arg_hint({}), "")
        self.assertEqual(_arg_hint(None), "")

    def test_narration_is_shown_as_plain_text(self):
        from apps.assistant.rendering import inline_text

        self.assertEqual(inline_text("**Serviceavtal** är *svagast*"), "Serviceavtal är svagast")

    def test_old_steps_without_state_still_render(self):
        """Rader sparade före den här ändringen ska inte se pågående ut."""
        user = get_user_model().objects.create_superuser("progress", password="x")
        self.client.force_login(user)
        job = AIJob.objects.create(user=user, title="Gammal")
        ChatMessage.objects.create(
            job=job,
            role=ChatRole.ASSISTANT,
            status="done",
            content="klar",
            steps=[{"op": "hamta_tjanst", "ok": True}],
        )
        html = self.client.get(reverse("manage:assistant_chat_job", args=[job.pk])).content.decode()
        self.assertIn("m-step--done", html)
        self.assertNotIn("m-step--running", html)


class StaleTurnTests(TestCase):
    """
    En tur körs i en tråd i webbprocessen. Startar processen om dör tråden
    och lämnar en PENDING-rad. Ingen fyller i den - därför sveparen.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("stale", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="Övergiven")
        self.reply = ChatMessage.objects.create(
            job=self.job, role=ChatRole.ASSISTANT, status=ChatMessage.Status.PENDING
        )

    def _age(self, seconds):
        """Sätt updated_at bakåt utan att auto_now skriver över det."""
        ChatMessage.objects.filter(pk=self.reply.pk).update(
            updated_at=timezone.now() - timedelta(seconds=seconds)
        )

    def test_polling_a_dead_turn_marks_it_failed(self):
        """
        Sveparen kördes tidigare bara när en NY tur startades - satt man och
        tittade på en död tur blev den aldrig utdömd.
        """
        self._age(tasks.STALE_AFTER + 60)
        data = self.client.get(reverse("manage:assistant_chat_poll", args=[self.job.pk])).json()
        self.assertEqual(data["status"], ChatMessage.Status.FAILED)
        self.assertIn("omstart", data["error"])

    def test_a_turn_still_making_progress_is_not_killed(self):
        """
        Staleness mäts på updated_at, inte created_at: en tur som loggar
        steg lever, hur länge den än hållit på.
        """
        ChatMessage.objects.filter(pk=self.reply.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        self._age(5)
        data = self.client.get(reverse("manage:assistant_chat_poll", args=[self.job.pk])).json()
        self.assertEqual(data["status"], ChatMessage.Status.PENDING)

    def test_ui_polls_longer_than_the_sweeper_waits(self):
        """
        Ger gränssnittet upp först, står "Arbetar" kvar för evigt. Den här
        ordningen är hela poängen och får inte kastas om av misstag.
        """
        import re
        from pathlib import Path

        from django.conf import settings

        template = (Path(settings.BASE_DIR) / "templates/manage/assistant/chat.html").read_text()
        match = re.search(r"tries > (\d+)", template)
        tries = int(match.group(1))
        # Pollintervallet är den FÖRSTA setInterval-takten efter brytpunkten;
        # söker man från filens början träffar man debouncen i @-menyn.
        interval = int(re.search(r"\}, (\d+)\);", template[match.end() :]).group(1))
        self.assertGreater(tries * interval / 1000, tasks.STALE_AFTER)


class ReviewButtonTests(TestCase):
    """Förslagen godkänns i chatten, inte via en resa till granskningssidan."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("cta", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="Med förslag")
        ChatMessage.objects.create(
            job=self.job, role=ChatRole.ASSISTANT, status="done", content="Klart."
        )
        category = ServiceCategory.objects.create(name="Vatten")
        self.service = Service.objects.create(
            category=category, name="Spolning", description="Gammal", is_active=True
        )

    def _draft(self):
        """Ett RIKTIGT förslag - ett med tom payload går inte att godkänna."""
        return draft.propose(
            self.job,
            "uppdatera_tjanst_text",
            {"slug": self.service.slug, "description": "Ny text"},
        )

    def test_draft_cards_appear_in_the_chat(self):
        """Godkänn där du står - inte via en resa till granskningssidan."""
        self._draft()
        html = self.client.get(
            reverse("manage:assistant_chat_job", args=[self.job.pk])
        ).content.decode()
        self.assertIn("m-inline-draft", html)
        self.assertIn(reverse("manage:assistant_job_bulk", args=[self.job.pk]), html)
        self.assertIn("Godkänn", html)

    def test_no_cards_without_drafts(self):
        html = self.client.get(
            reverse("manage:assistant_chat_job", args=[self.job.pk])
        ).content.decode()
        self.assertNotIn("m-inline-draft", html)

    def test_approving_from_the_chat_applies_the_change(self):
        change = self._draft()
        self.client.post(
            reverse("manage:assistant_job_bulk", args=[self.job.pk]),
            {"change_ids": [change.pk], "action": "approve"},
        )
        change.refresh_from_db()
        self.assertEqual(change.status, DraftChange.Status.APPLIED)

    def test_finish_does_not_put_the_url_in_the_text(self):
        """
        Naken URL blir oklickbar text när svaret renderas som markdown -
        och en knapp är rätt gränssnitt för turens viktigaste åtgärd.
        """
        import inspect

        from apps.assistant import chat

        self.assertNotIn("review_url(reply.job)", inspect.getsource(chat._finish))

    def test_legacy_url_line_is_stripped(self):
        """Gamla samtal ska inte visa den oklickbara raden."""
        from apps.assistant.rendering import message_html

        html = message_html(
            "Klart.\n\n18 utkast väntar på ditt godkännande: http://x/manage/ai/jobb/25/"
        )
        self.assertNotIn("utkast väntar", html)
        self.assertIn("Klart.", html)

    def test_other_links_are_untouched(self):
        """Bara granskningslänken behandlas särskilt."""
        from apps.assistant.rendering import message_html

        html = message_html("Se [Boverket](https://boverket.se/) för reglerna.")
        self.assertIn('href="https://boverket.se/"', html)
        self.assertNotIn("m-review-cta", html)


class BulkSelectionTests(TestCase):
    """
    Klumpmarkeringen delade tidigare upp sig i ett formulär per grupp, och
    nya objekt fick var sin grupp. Resultatet: knappen upprepades en gång
    per förslag, och kryssrutor i olika grupper hörde till olika formulär -
    så en klumpknapp skickade bara sin egen ruta.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("bulk", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="Klump")
        self.section = FAQSection.objects.create(title="Vanliga", slug="vanliga")

    def _faq(self, question):
        return draft.propose(
            self.job,
            "skapa_faq_fraga",
            {"sektion_slug": self.section.slug, "fraga": question, "svar": "Svar."},
        )

    def test_new_objects_share_one_group(self):
        for q in ("Fråga ett?", "Fråga två?", "Fråga tre?"):
            self._faq(q)
        html = self.client.get(reverse("manage:assistant_job", args=[self.job.pk])).content.decode()
        self.assertEqual(html.count("Nya FAQ-frågor"), 1)

    def test_one_bulk_form_for_the_whole_job(self):
        """Alla kryssrutor måste höra till SAMMA formulär."""
        import re

        for q in ("A?", "B?"):
            self._faq(q)
        html = self.client.get(reverse("manage:assistant_job", args=[self.job.pk])).content.decode()
        forms = set(re.findall(r'name="change_ids"[^>]*form="([^"]+)"', html))
        self.assertEqual(forms, {"bulk-form"})
        self.assertEqual(html.count('id="bulk-approve"'), 1)

    def test_approving_several_at_once_applies_all(self):
        ids = [self._faq(q).pk for q in ("A?", "B?", "C?")]
        self.client.post(
            reverse("manage:assistant_job_bulk", args=[self.job.pk]),
            {"change_ids": ids, "action": "approve"},
        )
        self.assertEqual(
            DraftChange.objects.filter(pk__in=ids, status=DraftChange.Status.APPLIED).count(), 3
        )
        self.assertEqual(FAQItem.objects.filter(section=self.section).count(), 3)

    def test_select_all_counts_every_pending_row(self):
        """Även affärsdata räknas in - allt väntande går att markera."""
        self._faq("A?")
        category = ServiceCategory.objects.create(name="Avlopp")
        service = Service.objects.create(category=category, name="Spol", is_active=True)
        draft.propose(self.job, "satt_tjanst_aktiv", {"slug": service.slug, "aktiv": False})
        html = self.client.get(reverse("manage:assistant_job", args=[self.job.pk])).content.decode()
        self.assertIn("Markera alla (2)", html)


class PreviewTests(TestCase):
    """
    Förhandsgranskning: applicera, rendera, rulla tillbaka.

    Det farliga är återställningen. Ett utkast får aldrig kunna publiceras
    av att någon tittade på det.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("preview", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="Förhandsgranskning")
        self.category = ServiceCategory.objects.create(name="Vatten")
        self.service = Service.objects.create(
            category=self.category, name="Spolning", description="Gammal text", is_active=True
        )
        # ADX: en tjänsts publika sida är en BlockPage med samma slug, och
        # beskrivningen renderas i tjänstelistan (svc_list-blocket).
        page = BlockPage.objects.create(title="Spolning", slug=self.service.slug, is_published=True)
        Block.objects.create(page=page, block_type="svc_list", data={}, order=0)

    def _text_change(self):
        return draft.propose(
            self.job,
            "uppdatera_tjanst_text",
            {"slug": self.service.slug, "description": "HELT NY TEXT"},
        )

    def test_preview_shows_the_change(self):
        from apps.assistant.preview import render_draft

        html = render_draft(self._text_change(), self.user)
        self.assertIn("HELT NY TEXT", html)

    def test_preview_does_not_write_to_the_database(self):
        from apps.assistant.preview import render_draft

        change = self._text_change()
        render_draft(change, self.user)
        self.service.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(self.service.description, "Gammal text")
        self.assertEqual(change.status, DraftChange.Status.PENDING)

    def test_previewing_a_creation_creates_nothing(self):
        from apps.assistant.preview import render_draft

        before = Service.objects.count()
        change = draft.propose(
            self.job,
            "skapa_tjanst",
            {"namn": "Ny för test", "beskrivning": "Kort.", "steg": [{"rubrik": "Steg"}]},
        )
        # ADX: en ny tjänst har ingen BlockPage än, så förhandsgranskningen
        # svarar ärligt att sidan saknas - och kärnkravet står kvar: en titt
        # får ALDRIG skapa något.
        from apps.assistant.preview import PreviewUnavailable

        with self.assertRaises(PreviewUnavailable):
            render_draft(change, self.user)
        self.assertEqual(Service.objects.count(), before)
        self.assertFalse(Service.objects.filter(name="Ny för test").exists())

    def test_rollback_happens_even_when_rendering_crashes(self):
        """
        Återställningen ligger i ett finally. Utan det publicerar en
        misslyckad rendering utkastet.
        """
        from apps.assistant import preview

        change = self._text_change()
        with patch.object(preview, "_render_public", side_effect=RuntimeError("boom")):
            with self.assertRaises(preview.PreviewUnavailable):
                preview.render_draft(change, self.user)
        self.service.refresh_from_db()
        self.assertEqual(self.service.description, "Gammal text")

    def test_preview_renders_as_an_anonymous_visitor(self):
        """
        Sidan ska visas som BESÖKAREN ser den - med en inloggad användare
        renderas redigeringsdocken ovanpå.
        """
        import inspect

        from apps.assistant import preview

        self.assertIn("AnonymousUser()", inspect.getsource(preview._render_public))

    def test_change_without_a_page_says_so(self):
        from apps.assistant.preview import PreviewUnavailable, render_draft

        section = FAQSection.objects.create(title="Vanliga", slug="vanliga")
        change = draft.propose(
            self.job,
            "skapa_faq_fraga",
            {"sektion_slug": section.slug, "fraga": "F?", "svar": "S."},
        )
        with self.assertRaises(PreviewUnavailable):
            render_draft(change, self.user)

    def test_other_users_draft_is_not_previewable(self):
        other = get_user_model().objects.create_superuser("annan3", password="x")
        theirs = AIJob.objects.create(user=other, title="Deras")
        change = DraftChange.objects.create(
            job=theirs,
            operation="uppdatera_tjanst_text",
            risk=Risk.TEXT,
            status=DraftChange.Status.PENDING,
            summary="x",
            payload={},
        )
        for name in ("assistant_change_preview", "assistant_change_preview_frame"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"manage:{name}", args=[change.pk]))
                self.assertEqual(response.status_code, 404)

    def test_frame_is_allowed_in_an_iframe(self):
        """
        Sajtens globala X-Frame-Options är DENY, så utan undantag vägrar
        webbläsaren visa förhandsgranskningen. Undantaget gäller bara den
        här vyn och bara samma origin.
        """
        change = self._text_change()
        response = self.client.get(
            reverse("manage:assistant_change_preview_frame", args=[change.pk])
        )
        self.assertEqual(response.headers.get("X-Frame-Options"), "SAMEORIGIN")

    def test_other_manage_pages_keep_deny(self):
        """Undantaget får inte spilla över på resten av /manage/."""
        for name in ("assistant_jobs", "assistant_chat", "dashboard"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"manage:{name}"))
                self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")

    def test_decision_buttons_sit_in_one_row(self):
        """
        Förhandsgranskningslänken låg utanför formuläret och hamnade på egen
        rad. Alla tre knapparna ska ligga i samma flex-behållare.
        """
        change = self._text_change()
        html = self.client.get(
            reverse("manage:assistant_job", args=[change.job_id])
        ).content.decode()
        actions = html[html.index('class="m-draft__actions"') :]
        actions = actions[: actions.index("</div>")]
        self.assertIn("Godkänn", actions)
        self.assertIn("Avslå", actions)
        self.assertIn("Förhandsgranska", actions)

    def test_frame_view_returns_a_message_instead_of_crashing(self):
        section = FAQSection.objects.create(title="Vanliga", slug="vanliga")
        change = draft.propose(
            self.job,
            "skapa_faq_fraga",
            {"sektion_slug": section.slug, "fraga": "F?", "svar": "S."},
        )
        response = self.client.get(
            reverse("manage:assistant_change_preview_frame", args=[change.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("förhandsgranska", response.content.decode().lower())


class AudienceDefaultTests(TestCase):
    """
    Alla aktiva målgrupper förbockade på nya tjänster - både i formuläret
    och via AI:n. En tom koppling gör tjänsten osynlig på målgruppssidorna
    utan att något syns fel.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("aud", password="x")
        self.category = ServiceCategory.objects.create(name="Vatten")
        self.job = AIJob.objects.create(user=self.user, title="Målgrupper")
        for name in ("Privatperson", "Företag"):
            Audience.objects.create(name=name, is_active=True)
        Audience.objects.create(name="Avvecklad", is_active=False)

    def test_new_form_preselects_active_audiences(self):
        from apps.manage.forms import ServiceForm

        picked = {a.name for a in ServiceForm().initial.get("audiences", [])}
        self.assertEqual(picked, {"Privatperson", "Företag"})
        self.assertNotIn("Avvecklad", picked)

    def test_existing_service_keeps_its_own_selection(self):
        """Förvalet får inte skriva över en befintlig, medvetet tom koppling."""
        from apps.manage.forms import ServiceForm

        service = Service.objects.create(category=self.category, name="Utan", is_active=True)
        self.assertEqual(list(ServiceForm(instance=service).initial.get("audiences") or []), [])

    def test_submitting_an_empty_selection_is_respected(self):
        """Att bocka av alla ska gå - förvalet gäller bara obundna formulär."""
        from apps.manage.forms import ServiceForm

        form = ServiceForm(
            data={
                "name": "Tom",
                "category": self.category.pk,
                "description": "x",
                "order": 1,
                "audiences": [],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(list(form.cleaned_data["audiences"]), [])

    def test_ai_created_service_gets_all_active_audiences(self):
        change = draft.propose(
            self.job,
            "skapa_tjanst",
            {"namn": "AI-tjänst", "beskrivning": "Kort.", "steg": [{"rubrik": "Steg"}]},
        )
        service = draft.approve(change, self.user)
        self.assertEqual({a.name for a in service.audiences.all()}, {"Privatperson", "Företag"})
        self.assertTrue(service.is_active)


class FaqSectionDependencyTests(TestCase):
    """
    Sektion och frågor i samma tur.

    Frågorna krävde tidigare en sektion som redan fanns i databasen - men
    sektionen är bara ett utkast tills den godkänts. Anropet misslyckades
    med "Okänd FAQ-sektion" och INGET utkast skapades, vilket är varför tio
    sektioner låg tomma (2026-08-21).
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("faqdep", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="FAQ")

    def _section_and_question(self):
        section = draft.propose(
            self.job, "skapa_faq_sektion", {"titel": "Om golvbrunnar", "beskrivning": "X"}
        )
        question = draft.propose(
            self.job,
            "skapa_faq_fraga",
            {
                "sektion_slug": section.payload["slug"],
                "fraga": "Hur ofta rensas den?",
                "svar": "En gång per år.",
            },
        )
        return section, question

    def test_question_for_a_drafted_section_is_accepted(self):
        section, question = self._section_and_question()
        self.assertEqual(question.depends_on_id, section.pk)

    def test_bulk_approve_applies_section_before_its_questions(self):
        section, question = self._section_and_question()
        self.client.post(
            reverse("manage:assistant_job_bulk", args=[self.job.pk]),
            {"change_ids": [question.pk, section.pk], "action": "approve"},
        )
        created = FAQSection.objects.get(slug=section.payload["slug"])
        self.assertEqual(
            list(created.items.values_list("question", flat=True)), ["Hur ofta rensas den?"]
        )

    def test_question_before_its_section_is_refused(self):
        """Ordningen bevakas även vid enskilt godkännande."""
        _section, question = self._section_and_question()
        with self.assertRaises(OperationError):
            draft.approve(question, self.user)

    def test_unknown_section_still_errors(self):
        with self.assertRaises(OperationError):
            draft.propose(
                self.job,
                "skapa_faq_fraga",
                {"sektion_slug": "finns-inte", "fraga": "F?", "svar": "S."},
            )

    def test_existing_section_needs_no_dependency(self):
        section = FAQSection.objects.create(title="Finns", slug="finns")
        change = draft.propose(
            self.job,
            "skapa_faq_fraga",
            {"sektion_slug": section.slug, "fraga": "F?", "svar": "S."},
        )
        self.assertIsNone(change.depends_on_id)
        draft.approve(change, self.user)
        self.assertEqual(section.items.count(), 1)


class ConsentGatingTests(TestCase):
    """Samtycket: inloggning krävs, men kunden är släppt sedan 2026-08-22."""

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("manage:oauth_consent"), {"client_id": "x"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_logged_in_customer_is_not_blocked_by_role(self):
        """En okänd klient ska ge klientfel - aldrig 'inte aktiverad'."""
        user = get_user_model().objects.create_user("kundy", password="x", is_staff=True)
        self.client.force_login(user)
        response = self.client.get(reverse("manage:oauth_consent"), {"client_id": "finns-ej"})
        self.assertNotIn("inte aktiverad", response.content.decode())


class DisconnectTests(TestCase):
    """
    "Koppla bort" raderar anslutningen helt.

    Tidigare återkallades bara tokens och klientraden stod kvar - det såg
    ut som att knappen inte gjorde något, och varje anslutningsförsök från
    Claude-appen registrerar dessutom en NY klient, så döda rader hopades.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("disc", password="x", is_staff=True)
        self.client.force_login(self.user)

    def _client(self, name="Claude"):
        from apps.assistant.oauth_models import OAuthClient

        return OAuthClient.objects.create(
            client_id=f"cid-{name}-{OAuthClient.objects.count()}",
            client_name=name,
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        )

    def test_disconnect_deletes_the_client_and_its_tokens(self):
        from apps.assistant.oauth_models import OAuthClient, OAuthToken

        row = self._client()
        OAuthToken.objects.create(
            client=row,
            user=self.user,
            token_hash="x",
            kind=OAuthToken.Kind.ACCESS,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.client.post(reverse("manage:assistant_oauth_disconnect", args=[row.pk]))
        self.assertFalse(OAuthClient.objects.filter(pk=row.pk).exists())
        self.assertFalse(OAuthToken.objects.filter(client_id=row.pk).exists())

    def test_disconnect_all_clears_everything(self):
        from apps.assistant.oauth_models import OAuthClient

        for i in range(4):
            self._client(f"Claude {i}")
        self.client.post(reverse("manage:assistant_oauth_disconnect_all"))
        self.assertEqual(OAuthClient.objects.count(), 0)

    def test_get_does_not_delete(self):
        from apps.assistant.oauth_models import OAuthClient

        row = self._client()
        response = self.client.get(reverse("manage:assistant_oauth_disconnect_all"))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(OAuthClient.objects.filter(pk=row.pk).exists())


class FaqLinkingTests(TestCase):
    """
    AI:n kunde skapa en FAQ men aldrig fästa den någonstans - operationen
    saknades. Underförslagen lovade dessutom "koppla den till tjänsten",
    alltså något modellen inte kunde göra (2026-08-23).
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("faqlink", password="x")
        self.job = AIJob.objects.create(user=self.user, title="FAQ-koppling")
        self.area = Area.objects.create(name="Bromma", level=AreaLevel.DISTRICT)
        category = ServiceCategory.objects.create(name="Vatten")
        self.service = Service.objects.create(category=category, name="Spolning", is_active=True)

    def test_links_existing_section_to_area(self):
        section = FAQSection.objects.create(title="Frågor om Bromma", slug="fragor-bromma")
        change = draft.propose(
            self.job,
            "koppla_faq_till_omrade",
            {"omrade_slug": self.area.slug, "faq_slug": section.slug},
        )
        draft.approve(change, self.user)
        self.area.refresh_from_db()
        self.assertEqual(self.area.faq_section_id, section.pk)

    def test_links_a_section_proposed_in_the_same_turn(self):
        """Hela poängen: skapa FAQ och koppla den utan att vänta på godkännande."""
        section = draft.propose(
            self.job, "skapa_faq_sektion", {"titel": "Frågor om Bromma", "beskrivning": "X"}
        )
        link = draft.propose(
            self.job,
            "koppla_faq_till_omrade",
            {"omrade_slug": self.area.slug, "faq_slug": section.payload["slug"]},
        )
        self.assertEqual(link.depends_on_id, section.pk)
        draft.approve(section, self.user)
        draft.approve(link, self.user)
        self.area.refresh_from_db()
        self.assertEqual(self.area.faq_section.slug, section.payload["slug"])

    def test_links_to_service_too(self):
        section = FAQSection.objects.create(title="Om spolning", slug="om-spolning")
        change = draft.propose(
            self.job,
            "koppla_faq_till_tjanst",
            {"slug": self.service.slug, "faq_slug": section.slug},
        )
        draft.approve(change, self.user)
        self.service.refresh_from_db()
        self.assertEqual(self.service.faq_section_id, section.pk)

    def test_unknown_section_errors(self):
        with self.assertRaises(OperationError):
            draft.propose(
                self.job,
                "koppla_faq_till_omrade",
                {"omrade_slug": self.area.slug, "faq_slug": "finns-inte"},
            )


class WithdrawDraftTests(TestCase):
    """
    Så här ändrar modellen ett liggande förslag: drar tillbaka och lägger
    nytt. Att mutera på plats vore värre - before-ögonblicksbilden och
    kundens redan lästa diff skulle sluta stämma.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("withdraw", password="x")
        self.job = AIJob.objects.create(user=self.user, title="Ångra")
        category = ServiceCategory.objects.create(name="Vatten")
        self.service = Service.objects.create(
            category=category, name="Spolning", description="Gammal", is_active=True
        )

    def _draft(self):
        return draft.propose(
            self.job,
            "uppdatera_tjanst_text",
            {"slug": self.service.slug, "description": "Ny text"},
        )

    def test_withdraw_rejects_the_draft(self):
        target = self._draft()
        change = draft.propose(self.job, "dra_tillbaka_utkast", {"utkast_id": target.pk})
        draft.approve(change, self.user)
        target.refresh_from_db()
        self.assertEqual(target.status, DraftChange.Status.REJECTED)

    def test_cannot_withdraw_another_users_draft(self):
        other = get_user_model().objects.create_user("annan9", password="x")
        theirs = AIJob.objects.create(user=other, title="Deras")
        row = DraftChange.objects.create(
            job=theirs,
            operation="uppdatera_tjanst_text",
            risk=Risk.TEXT,
            status=DraftChange.Status.PENDING,
            summary="x",
            payload={},
        )
        with self.assertRaises(OperationError):
            draft.propose(self.job, "dra_tillbaka_utkast", {"utkast_id": row.pk})

    def test_cannot_withdraw_an_approved_change(self):
        target = self._draft()
        draft.approve(target, self.user)
        with self.assertRaises(OperationError):
            draft.propose(self.job, "dra_tillbaka_utkast", {"utkast_id": target.pk})


class DraftMentionTests(TestCase):
    """@utkast:<id> - peka på ett liggande förslag i stället för att beskriva det."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("dm", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="Utkast")
        self.change = DraftChange.objects.create(
            job=self.job,
            operation="uppdatera_tjanst_text",
            risk=Risk.TEXT,
            status=DraftChange.Status.PENDING,
            summary="Textändring: Byte av blandare",
            payload={},
        )

    def test_pending_drafts_are_searchable(self):
        results = self.client.get(
            reverse("manage:assistant_mention_search"), {"q": "blandare"}
        ).json()["results"]
        self.assertTrue(
            any(r["typ"] == "utkast" and r["ref"] == str(self.change.pk) for r in results)
        )

    def test_token_resolves_with_a_hint_to_withdraw(self):
        from apps.assistant import mentions

        ctx = mentions.context_for(f"ändra @utkast:{self.change.pk}", self.user)
        self.assertIn("Byte av blandare", ctx)
        self.assertIn("dra_tillbaka_utkast", ctx)

    def test_other_users_drafts_are_not_searchable(self):
        other = get_user_model().objects.create_user("annan10", password="x")
        theirs = AIJob.objects.create(user=other, title="Deras")
        DraftChange.objects.create(
            job=theirs,
            operation="uppdatera_tjanst_text",
            risk=Risk.TEXT,
            status=DraftChange.Status.PENDING,
            summary="Hemligt blandare",
            payload={},
        )
        results = self.client.get(
            reverse("manage:assistant_mention_search"), {"q": "Hemligt"}
        ).json()["results"]
        self.assertEqual(results, [])


class ReadVisibilityTests(TestCase):
    """
    Modellen ska inte behöva gissa. Den antog att områdesrubriken genereras
    automatiskt (den gjorde det - men gissningen var otur, inte kunskap),
    och den såg inte sina egna kopplingar, så den kunde föreslå en koppling
    som redan fanns (2026-08-23).
    """

    def test_area_read_explains_the_generated_heading(self):
        from apps.assistant.operations import REGISTRY

        area = Area.objects.create(name="Bromma", level=AreaLevel.DISTRICT, heading="")
        data = REGISTRY["hamta_omrade"].read(None, slug=area.slug)
        self.assertEqual(data["rubrik"], "")
        self.assertEqual(data["rubrik_som_visas"], "Rörmokare i Bromma")
        self.assertTrue(data["rubrik_autogenererad"])

    def test_set_heading_is_reported_as_not_generated(self):
        from apps.assistant.operations import REGISTRY

        area = Area.objects.create(name="Solna", level=AreaLevel.DISTRICT, heading="VVS i Solna")
        data = REGISTRY["hamta_omrade"].read(None, slug=area.slug)
        self.assertEqual(data["rubrik_som_visas"], "VVS i Solna")
        self.assertFalse(data["rubrik_autogenererad"])

    def test_area_read_shows_its_links(self):
        from apps.assistant.operations import REGISTRY

        area = Area.objects.create(name="Bromma", level=AreaLevel.DISTRICT)
        section = FAQSection.objects.create(title="Frågor", slug="fragor")
        area.faq_section = section
        area.save(update_fields=["faq_section"])
        data = REGISTRY["hamta_omrade"].read(None, slug=area.slug)
        self.assertEqual(data["faq_sektion"], "fragor")
        self.assertIn("tjanster", data)
        self.assertIn("grannomraden", data)

    def test_service_read_shows_its_links(self):
        from apps.assistant.operations import REGISTRY

        category = ServiceCategory.objects.create(name="Vatten")
        service = Service.objects.create(category=category, name="Spolning", is_active=True)
        section = FAQSection.objects.create(title="Om spolning", slug="om-spolning")
        service.faq_section = section
        service.save(update_fields=["faq_section"])
        data = REGISTRY["hamta_tjanst"].read(None, slug=service.slug)
        self.assertEqual(data["faq_sektion"], "om-spolning")
        self.assertIn("malgrupper", data)
        self.assertIn("omraden", data)


class NewCapabilityTests(TestCase):
    """
    Luckorna från fältrevisionen 2026-08-23: FAQ-sektioner gick att skapa
    men aldrig ändra, sidor kunde inte publiceras, tjänstekopplingar inte
    tas bort och grannområden saknades helt.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("caps", password="x")
        self.job = AIJob.objects.create(user=self.user, title="Nya förmågor")
        self.category = ServiceCategory.objects.create(name="Vatten")
        self.service = Service.objects.create(
            category=self.category, name="Spolning", is_active=True
        )
        self.area = Area.objects.create(name="Bromma", level=AreaLevel.DISTRICT)

    def test_update_faq_section(self):
        section = FAQSection.objects.create(title="Gammal", slug="gammal")
        change = draft.propose(
            self.job,
            "uppdatera_faq_sektion",
            {"sektion_slug": section.slug, "titel": "Ny titel"},
        )
        draft.approve(change, self.user)
        section.refresh_from_db()
        self.assertEqual(section.title, "Ny titel")

    def test_hide_faq_section(self):
        section = FAQSection.objects.create(title="Tom", slug="tom", is_active=True)
        change = draft.propose(
            self.job, "satt_faq_sektion_aktiv", {"sektion_slug": section.slug, "aktiv": False}
        )
        draft.approve(change, self.user)
        section.refresh_from_db()
        self.assertFalse(section.is_active)

    def test_publish_a_page(self):
        page = BlockPage.objects.create(title="Ny sida", slug="ny-sida", is_published=False)
        change = draft.propose(
            self.job, "satt_sida_publicerad", {"slug": page.slug, "publicerad": True}
        )
        draft.approve(change, self.user)
        page.refresh_from_db()
        self.assertTrue(page.is_published)

    def test_set_neighbours_replaces_the_whole_list(self):
        a = Area.objects.create(name="Solna", level=AreaLevel.DISTRICT)
        b = Area.objects.create(name="Sundbyberg", level=AreaLevel.DISTRICT)
        self.area.neighbours.set([a])
        change = draft.propose(
            self.job, "satt_grannomraden", {"slug": self.area.slug, "grannar": [b.slug]}
        )
        draft.approve(change, self.user)
        self.assertEqual([n.slug for n in self.area.neighbours.all()], [b.slug])

    def test_area_cannot_neighbour_itself(self):
        with self.assertRaises(OperationError):
            draft.propose(
                self.job, "satt_grannomraden", {"slug": self.area.slug, "grannar": [self.area.slug]}
            )


class StyleGuideViewTests(TestCase):
    """
    Skrivguiden fanns som fält på SiteSettings men saknade gränssnitt helt -
    den gick alltså inte att redigera (upptäckt 2026-08-23).
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("guide", password="x", is_staff=True)
        self.client.force_login(self.user)

    def test_page_is_reachable_from_the_subnav(self):
        html = self.client.get(reverse("manage:assistant_chat")).content.decode()
        self.assertIn(reverse("manage:assistant_style_guide"), html)

    def test_saving_changes_what_the_ai_reads(self):
        from apps.assistant.operations import REGISTRY

        self.client.post(
            reverse("manage:assistant_style_guide"),
            {"ai_style_guide": "Skriv kort. Aldrig 'givetvis'."},
        )
        guide = REGISTRY["hamta_skrivguide"].read(self.user)
        self.assertEqual(guide["skrivguide"], "Skriv kort. Aldrig 'givetvis'.")
        self.assertFalse(guide["ar_standard"])

    def test_clearing_restores_the_default(self):
        from apps.assistant.operations import REGISTRY

        self.client.post(reverse("manage:assistant_style_guide"), {"ai_style_guide": "Något"})
        self.client.post(reverse("manage:assistant_style_guide"), {"ai_style_guide": "   "})
        self.assertTrue(REGISTRY["hamta_skrivguide"].read(self.user)["ar_standard"])


class DoorwayPageGuardTests(TestCase):
    """
    AI:n får inte koppla tjänster till orter (kundens beslut 2026-08-23).

    Kombinationssidorna renderar samma tjänstetext och samma ortstext med
    ortsnamnet inbytt i rubriken. 5 tjänster x 252 områden vore 1 260
    nästan identiska sidor - doorway pages som Google straffar. En modell
    som kopplar på uppmaning skalar misstaget på sekunder.
    """

    def test_no_operation_can_link_services_to_areas(self):
        from apps.assistant.operations import all_operations

        names = {op.name for op in all_operations()}
        self.assertNotIn("koppla_tjanst_till_omrade", names)
        self.assertNotIn("koppla_bort_tjanst_fran_omrade", names)

    def test_no_operation_touches_the_areaservice_table(self):
        """Bredare spärr: ingen operation alls får skriva kopplingsraderna."""
        import inspect

        from apps.areas.models import AreaService
        from apps.assistant.operations import all_operations

        for op in all_operations():
            for func in (op.prepare, op.apply):
                if func is None:
                    continue
                try:
                    source = inspect.getsource(func)
                except (OSError, TypeError):
                    continue
                with self.subTest(op=op.name):
                    self.assertNotIn(
                        "AreaService.objects.create", source, f"{op.name} skapar kopplingsrader"
                    )
        self.assertEqual(AreaService.objects.count(), 0)


class ImagePresenceTests(TestCase):
    """
    "Vilka tjänster saknar bilder?" gick inte att svara på - modellen såg
    inga bildfält alls och trodde att den saknade verktyg (2026-08-23).
    Den behöver inte SE bilden, bara veta om den finns.
    """

    def setUp(self):
        from apps.website.models import MediaFile

        self.media = MediaFile.objects.create(
            file="media/test.jpg", original_filename="test.jpg", alt_text="En kran"
        )
        self.category = ServiceCategory.objects.create(name="Vatten")

    def test_service_list_flags_missing_images(self):
        from apps.assistant.operations import REGISTRY

        Service.objects.create(category=self.category, name="Utan bild", is_active=True)
        Service.objects.create(
            category=self.category, name="Med bild", is_active=True, image=self.media
        )
        rows = {r["namn"]: r["har_bild"] for r in REGISTRY["lista_tjanster"].read(None)["tjanster"]}
        self.assertFalse(rows["Utan bild"])
        self.assertTrue(rows["Med bild"])

    def test_service_list_includes_categories_with_image_status(self):
        from apps.assistant.operations import REGISTRY

        data = REGISTRY["lista_tjanster"].read(None)
        names = {c["namn"]: c["har_bild"] for c in data["kategorier"]}
        self.assertIn("Vatten", names)
        self.assertFalse(names["Vatten"])

    def test_service_read_reports_alt_text(self):
        """Alt-texten är det enda modellen kan bedöma om bilden."""
        from apps.assistant.operations import REGISTRY

        service = Service.objects.create(
            category=self.category, name="Med bild", is_active=True, image=self.media
        )
        data = REGISTRY["hamta_tjanst"].read(None, slug=service.slug)
        self.assertEqual(data["bild"]["alt_text"], "En kran")

    def test_area_read_and_list_report_images(self):
        from apps.assistant.operations import REGISTRY

        area = Area.objects.create(name="Bromma", level=AreaLevel.MUNICIPALITY)
        self.assertIsNone(REGISTRY["hamta_omrade"].read(None, slug=area.slug)["bild"])
        rows = {r["namn"]: r["har_bild"] for r in REGISTRY["lista_omraden"].read(None)["omraden"]}
        self.assertFalse(rows["Bromma"])

    def test_block_reports_whether_it_has_an_image(self):
        from apps.assistant.operations import REGISTRY
        from apps.website.models import Block, BlockPage

        page = BlockPage.objects.create(title="Sida", slug="sida")
        Block.objects.create(page=page, block_type="article", data={}, order=1)
        Block.objects.create(
            page=page, block_type="article", data={"image_id": self.media.pk}, order=2
        )
        blocks = REGISTRY["hamta_sida"].read(None, slug=page.slug)["block"]
        self.assertEqual([b["har_bild"] for b in blocks], [False, True])


class DependencyGroupingTests(TestCase):
    """
    En FAQ-sektion och dess frågor är EN sak att godkänna, inte sex
    (kundens önskemål 2026-08-23). Grupperingen följer depends_on-kedjan.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("depgrp", password="x")
        self.client.force_login(self.user)
        self.job = AIJob.objects.create(user=self.user, title="FAQ")

    def _faq_with_questions(self, count=3):
        section = draft.propose(
            self.job, "skapa_faq_sektion", {"titel": "Frågor om Bromma", "beskrivning": "X"}
        )
        for i in range(count):
            draft.propose(
                self.job,
                "skapa_faq_fraga",
                {"sektion_slug": section.payload["slug"], "fraga": f"Fråga {i}?", "svar": "Svar."},
            )
        return section

    def test_section_and_questions_form_one_group(self):
        from apps.assistant.views import _change_rows

        self._faq_with_questions()
        groups, _decided = _change_rows(self.job)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 4)

    def test_review_page_offers_one_button_for_the_whole_faq(self):
        self._faq_with_questions()
        html = self.client.get(reverse("manage:assistant_job", args=[self.job.pk])).content.decode()
        self.assertIn("Godkänn alla 4", html)

    def test_approving_the_group_creates_section_and_questions(self):
        section = self._faq_with_questions()
        ids = [c.pk for c in self.job.changes.all()]
        self.client.post(
            reverse("manage:assistant_job_bulk", args=[self.job.pk]),
            {"change_ids": ids, "action": "approve"},
        )
        created = FAQSection.objects.get(slug=section.payload["slug"])
        self.assertEqual(created.items.count(), 3)

    def test_independent_drafts_stay_in_their_own_groups(self):
        from apps.assistant.views import _change_rows

        category = ServiceCategory.objects.create(name="Vatten")
        service = Service.objects.create(
            category=category, name="Spolning", description="Gammal", is_active=True
        )
        self._faq_with_questions()
        draft.propose(
            self.job, "uppdatera_tjanst_text", {"slug": service.slug, "description": "Ny"}
        )
        groups, _decided = _change_rows(self.job)
        self.assertEqual(len(groups), 2)


class DeepLinkTests(TestCase):
    """MCP-svaret pekar på ETT förslag, inte på listan."""

    def test_draft_url_points_at_the_single_draft(self):
        from django.test import override_settings

        from apps.assistant.runtime import draft_url

        user = get_user_model().objects.create_superuser("deep", password="x")
        job = AIJob.objects.create(user=user, title="Djuplänk")
        change = DraftChange.objects.create(
            job=job,
            operation="x",
            risk=Risk.TEXT,
            status=DraftChange.Status.PENDING,
            summary="s",
            payload={},
        )
        with override_settings(SITE_BASE_URL="https://www.adx.se"):
            url = draft_url(change)
        self.assertTrue(url.startswith("https://www.adx.se"))
        self.assertTrue(url.endswith(f"#utkast-{change.pk}"))

    def test_card_carries_the_anchor(self):
        user = get_user_model().objects.create_superuser("deep2", password="x")
        self.client.force_login(user)
        job = AIJob.objects.create(user=user, title="Ankare")
        category = ServiceCategory.objects.create(name="Vatten")
        service = Service.objects.create(
            category=category, name="Spolning", description="G", is_active=True
        )
        change = draft.propose(
            job, "uppdatera_tjanst_text", {"slug": service.slug, "description": "Ny"}
        )
        html = self.client.get(reverse("manage:assistant_job", args=[job.pk])).content.decode()
        self.assertIn(f'id="utkast-{change.pk}"', html)
