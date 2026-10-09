"""Utskick i verktyget (README I.1, I.4 till I.10, F.3, F.8, H.1, H.5, J S2):
listan, guiden, Granska, bekräftelsen, läget, rapporten, mottagarna,
testsms:et, inställningarna och sms-texten (composer.py, reports.py).

    TenancyTests        varje adress är kontots och 404 annars, också när utskick är av
    ForeignIdTests      varje id ur en förfrågan prövas mot kontot (400)
    GuideTests          stegen sparar, ett schemalagt blir utkast vid ändring
    ReviewTests         Granskas kontroller, nonce och byråns kryssruta (I.4)
    StateTests          pausa, fortsätt, avbryt, Granska igen, Be ADX
    ReportTests         rapportens siffror, mottagarna och Spara som lista
    TestSendTests       testsms:et (F.8): vart, hur många, aldrig demot
    SettingsTests       Inställningar för utskick (I.9, S2)
    ComposerTests       platshållarna, avregistreringen, koderna och mallarna
    GuardTests          375 px, räknarens kontrakt och mallarnas text

Inget når nätet: apps.sms.elks._post är FakeElks där något skickas.
"""

import ast
import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.common.tests import PromiseGuardTests
from apps.flamingo.models import Campaign, Lead, Service
from apps.flamingo.pagebuilder.ai import SPEED, URGENCY
from apps.sms import encoding
from apps.sms.models import SmsAccount, SmsMessage
from apps.sms.tests import FakeElks

from . import composer, links, reports
from . import consent as consents
from .app_views import utskick as views
from .models import (
    CHANNEL_SMS,
    INFORMATION,
    REKLAM,
    AllowedHost,
    Click,
    Consent,
    ContactList,
    Event,
    FieldDef,
    LinkCode,
    ListMembership,
    Recipient,
    Switchboard,
    Tag,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from .testing import UtskickFixture, make_contact

BASE = Path(settings.BASE_DIR)
TEMPLATES = BASE / "templates" / "flamingo" / "app" / "utskick"
REPLY = "+46766860046"
LIVE = override_settings(
    SMS_SEND_LIVE=True,
    ELKS_API_USERNAME="test",
    ELKS_API_PASSWORD="test-losen",
    SMS_PROVIDER="46elks",
    SMS_CALLBACK_BASE_URL="",
    UTSKICK_REPLY_NUMBER=REPLY,
    INQUIRY_NOTIFICATION_EMAIL="byran@adx.example",
)
BODY = "Hej {förnamn|du}, dags för service hos Exempelrör. Boka: {länk:boka}"
#: Adresserna i utskicket: (namn, tar pk, metod).
ROUTES = [
    ("flamingo:app_utskick_list", False, "get"),
    ("flamingo:app_utskick_new", False, "post"),
    ("flamingo:app_utskick_settings", False, "get"),
    ("flamingo:app_utskick", True, "get"),
    ("flamingo:app_utskick_count", True, "get"),
    ("flamingo:app_utskick_sms_preview", True, "get"),
    ("flamingo:app_utskick_link_check", True, "post"),
    ("flamingo:app_utskick_test", True, "post"),
    ("flamingo:app_utskick_confirm", True, "post"),
    ("flamingo:app_utskick_state", True, "post"),
    ("flamingo:app_utskick_recipients", True, "get"),
    ("flamingo:app_utskick_save_list", True, "post"),
]
STEPS = [key for key, _label in views.STEPS]


def phone(n):
    """PTS fiktiva serie +4670174xxxx."""
    return f"+4670174{n:04d}"


class UiFixture(UtskickFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sms_account = SmsAccount.objects.create(
            customer=cls.customer, is_enabled=True, sender_name="Exempelror"
        )
        # Kundens egen webbplats: länkar dit behöver inget godkännande (E.8).
        cls.account.website_url = "https://exempelror.example"
        cls.account.save(update_fields=["website_url"])
        cls.kunder = ContactList.objects.create(account=cls.account, name="Kunder")
        cls.tag = Tag.objects.create(account=cls.account, name="Bromma")
        cls.foreign_list = ContactList.objects.create(account=cls.other_account, name="Hemlig")
        cls.foreign_tag = Tag.objects.create(account=cls.other_account, name="Hemlig")
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK,
            defaults={
                "sms_enabled": True,
                "links_ready_at": timezone.now(),
                "sms_inbound_ready_at": timezone.now(),
            },
        )

    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)
        self.n = 0

    def person(self, status=consents.YES, account=None, contact_list=None, **data):
        account = account or self.account
        self.n += 1
        data.setdefault("first_name", f"Person{self.n}")
        data.setdefault("phone", phone(1000 + self.n + (500 if account != self.account else 0)))
        kontakt = make_contact(account, **data)
        if status in (consents.YES, consents.EXISTING):
            consents.set_status(
                kontakt, CHANNEL_SMS, status, source=Consent.Source.MANUAL, evidence="kassan"
            )
        target = contact_list or (self.kunder if account == self.account else None)
        if target is not None:
            target.memberships.create(contact=kontakt)
        return kontakt

    def utskick(self, account=None, body=BODY, link=True, **kwargs):
        account = account or self.account
        kwargs.setdefault(
            "audience", {"lists": [self.kunder.pk]} if account == self.account else {}
        )
        u = Utskick.objects.create(
            account=account, name="Höstservice värmepump", sms_body=body, **kwargs
        )
        if link and "{länk:boka}" in body:
            TrackedLink.objects.create(
                account=account,
                utskick=u,
                kind=TrackedLink.Kind.EXTERNAL,
                key="boka",
                destination="https://exempelror.example/boka",
            )
        return u

    def url(self, name, utskick=None, **kwargs):
        if utskick is None:
            return reverse(name)
        return reverse(name, args=[utskick.pk], **kwargs)

    def step(self, utskick, step):
        return reverse("flamingo:app_utskick_step", args=[utskick.pk, step])

    def granska_nonce(self, utskick, client=None):
        response = (client or self.client).get(self.step(utskick, "granska"))
        self.assertEqual(response.status_code, 200)
        return response.context["nonce"]


# ---------------------------------------------------------------------------
# Behörighet (H.1)
# ---------------------------------------------------------------------------


class TenancyTests(UiFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.own = self.utskick()
        self.foreign = self.utskick(account=self.other_account)

    def test_another_accounts_utskick_is_404_on_every_route(self):
        for name, with_pk, method in ROUTES:
            if not with_pk:
                continue
            with self.subTest(name=name):
                url = self.url(name, self.foreign)
                self.assertEqual(getattr(self.client, method)(url).status_code, 404)
        for step in STEPS:
            with self.subTest(step=step):
                self.assertEqual(self.client.get(self.step(self.foreign, step)).status_code, 404)
                self.assertEqual(self.client.post(self.step(self.foreign, step)).status_code, 404)
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.status, Utskick.Status.DRAFT)

    def test_everything_is_404_when_utskick_is_off_also_for_staff(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        for client in (self.client, self.client_for(self.staff)):
            for name, with_pk, method in ROUTES:
                url = self.url(name, self.own if with_pk else None)
                with self.subTest(name=name):
                    self.assertEqual(getattr(client, method)(url).status_code, 404)

    def test_every_page_renders_for_the_customer_and_for_staff_in_view_as(self):
        self.person()
        for client in (self.client, self.client_for(self.staff)):
            for name, with_pk, method in ROUTES:
                if method != "get":
                    continue
                with self.subTest(name=name):
                    response = client.get(self.url(name, self.own if with_pk else None))
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, "Hemlig", status_code=200)
            for step in STEPS:
                with self.subTest(step=step):
                    response = client.get(self.step(self.own, step))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context["app_active"], "utskick")

    def test_the_list_shows_only_the_accounts_utskick(self):
        self.foreign.name = "Hemligt utskick"
        self.foreign.save()
        response = self.client.get(self.url("flamingo:app_utskick_list"))
        self.assertContains(response, "Höstservice värmepump")
        self.assertNotContains(response, "Hemligt utskick")

    def test_a_running_utskick_cannot_be_edited(self):
        Utskick.objects.filter(pk=self.own.pk).update(status=Utskick.Status.SENDING)
        response = self.client.post(self.step(self.own, "innehall"), {"sms_body": "Ny text"})
        self.assertRedirects(response, self.url("flamingo:app_utskick", self.own))
        self.own.refresh_from_db()
        self.assertEqual(self.own.sms_body, BODY)


class ForeignIdTests(UiFixture, TestCase):
    """Varje id ur en förfrågans kropp eller adress går genom owned_ids:
    400 och ingen ändring när ett enda är främmande (H.1)."""

    def setUp(self):
        super().setUp()
        self.own = self.utskick(audience={})
        self.foreign_contact = self.person(account=self.other_account)

    def test_audience_with_a_foreign_list_tag_or_contact_is_400(self):
        for field, value in (
            ("lists", self.foreign_list.pk),
            ("tags", self.foreign_tag.pk),
            ("contacts", self.foreign_contact.pk),
            ("exclude_lists", self.foreign_list.pk),
            ("exclude_tags", self.foreign_tag.pk),
        ):
            with self.subTest(field=field):
                response = self.client.post(
                    self.step(self.own, "mottagare"),
                    {"namn": "X", "lists": [self.kunder.pk], field: value},
                )
                self.assertEqual(response.status_code, 400)
        self.own.refresh_from_db()
        self.assertEqual(self.own.audience, {})

    def test_the_live_count_refuses_a_foreign_id(self):
        url = self.url("flamingo:app_utskick_count", self.own)
        self.assertEqual(self.client.get(url, {"lists": self.foreign_list.pk}).status_code, 400)
        self.assertEqual(self.client.get(url, {"lists": "1 OR 1=1"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"lists": self.kunder.pk}).status_code, 200)

    def test_preview_contact_link_campaign_and_list_are_checked(self):
        preview = self.url("flamingo:app_utskick_sms_preview", self.own)
        self.assertEqual(
            self.client.get(preview, {"kontakt": self.foreign_contact.pk}).status_code, 400
        )
        service = Service.objects.create(account=self.other_account, name="Hemlig tjänst")
        campaign = Campaign.objects.create(
            account=self.other_account, service=service, name="Hemlig", page_slug="hemlig-sida"
        )
        response = self.client.post(
            self.step(self.own, "innehall"),
            {
                "sms_body": "Hej",
                "action": "lank",
                "lank_nyckel": "kampanj",
                "lank_kampanj": campaign.pk,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TrackedLink.objects.filter(utskick=self.own, key="kampanj").exists())
        foreign_link = TrackedLink.objects.create(
            account=self.other_account,
            utskick=self.utskick(account=self.other_account),
            kind=TrackedLink.Kind.EXTERNAL,
            key="x",
            destination="https://annan.example/",
        )
        response = self.client.post(
            self.step(self.own, "innehall"), {"sms_body": "Hej", "ta_bort_lank": foreign_link.pk}
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(TrackedLink.objects.filter(pk=foreign_link.pk).exists())
        response = self.client.post(
            self.url("flamingo:app_utskick_save_list", self.own),
            {"visa": "alla", "lista_id": self.foreign_list.pk},
        )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# Guiden
# ---------------------------------------------------------------------------


class GuideTests(UiFixture, TestCase):
    def test_new_creates_a_draft_and_opens_the_first_step(self):
        response = self.client.post(self.url("flamingo:app_utskick_new"))
        utskick = Utskick.objects.get(account=self.account)
        self.assertRedirects(response, self.step(utskick, "mottagare"))
        self.assertEqual(utskick.status, Utskick.Status.DRAFT)
        self.assertEqual(utskick.channel_mode, Utskick.ChannelMode.SMS_ONLY)
        self.assertEqual(utskick.created_by, self.anna)
        self.assertFalse(Utskick.objects.filter(account=self.other_account).exists())

    def test_the_audience_is_saved_and_counted(self):
        self.person()
        self.person()
        self.person(status=consents.MISSING)
        utskick = self.utskick(audience={})
        response = self.client.post(
            self.step(utskick, "mottagare"),
            {
                "namn": "Däckbyte",
                "lists": [self.kunder.pk],
                "exclude_recent": "1",
                "nasta": "kanal",
            },
        )
        self.assertRedirects(response, self.step(utskick, "kanal"))
        utskick.refresh_from_db()
        self.assertEqual(utskick.name, "Däckbyte")
        self.assertEqual(utskick.audience["lists"], [self.kunder.pk])
        self.assertEqual(utskick.audience["exclude"]["recent_days"], 14)
        data = self.client.get(self.url("flamingo:app_utskick_count", utskick)).json()
        self.assertEqual((data["total"], data["sms"], data["skipped"]), (3, 2, 1))
        self.assertEqual(data["text"], "2 får sms. 1 hoppas över: 1 utan samtycke.")
        # Det osparade urvalet räknas också (guiden räknar medan kunden kryssar).
        data = self.client.get(
            self.url("flamingo:app_utskick_count", utskick), {"urval": "1", "tags": self.tag.pk}
        ).json()
        self.assertEqual(data["total"], 0)

    def test_an_empty_audience_is_not_a_next_step(self):
        utskick = self.utskick(audience={})
        response = self.client.post(
            self.step(utskick, "mottagare"), {"namn": "X", "nasta": "kanal"}
        )
        self.assertRedirects(
            response, self.step(utskick, "mottagare"), fetch_redirect_response=False
        )
        page = self.client.get(self.step(utskick, "mottagare"))
        self.assertContains(page, views.EMPTY_AUDIENCE)

    def test_search_keeps_the_selection_and_lists_matches(self):
        kontakt = self.person(
            first_name="Sigrid",
            contact_list=ContactList.objects.create(account=self.account, name="Annan"),
        )
        utskick = self.utskick(audience={})
        response = self.client.post(
            self.step(utskick, "mottagare"),
            {"namn": "X", "lists": [self.kunder.pk], "action": "sok", "sok": "sigrid"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("sok=sigrid", response["Location"])
        utskick.refresh_from_db()
        self.assertEqual(utskick.audience["lists"], [self.kunder.pk])
        page = self.client.get(response["Location"])
        self.assertContains(page, f'name="contacts" value="{kontakt.pk}"')

    def test_channel_step_information_needs_a_reason_and_names_must_be_approved(self):
        utskick = self.utskick()
        url = self.step(utskick, "kanal")
        response = self.client.post(url, {"syfte": INFORMATION, "avsandare": "reply"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Välj varför du skickar information.")
        response = self.client.post(
            url, {"syfte": INFORMATION, "info_reason": "annat", "avsandare": "reply"}
        )
        self.assertContains(response, "Skriv varför du skickar information.")
        response = self.client.post(url, {"syfte": REKLAM, "avsandare": "name:Okand"})
        self.assertContains(response, "Välj en avsändare.")
        response = self.client.post(
            url,
            {"syfte": INFORMATION, "info_reason": "oppettider", "avsandare": "name:Exempelror"},
        )
        self.assertRedirects(response, self.step(utskick, "innehall"))
        utskick.refresh_from_db()
        self.assertEqual(utskick.purpose, INFORMATION)
        self.assertEqual(utskick.info_reason, "oppettider")
        self.assertEqual(utskick.sms_sender_kind, Utskick.SenderKind.NAME)
        self.assertEqual(utskick.sms_sender_name, "Exempelror")

    def test_channel_step_explains_locked_options(self):
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(is_enabled=False, sender_name="")
        response = self.client.get(self.step(self.utskick(), "kanal"))
        self.assertContains(response, "Sms är inte aktiverat för dig. Be ADX slå på det.")
        self.assertContains(response, "ADX godkänner avsändarnamn. Be ADX lägga till ett.")
        self.assertContains(response, "0766 86 00 46")
        self.assertNotContains(response, "Bara e-post")

    def test_content_saves_text_fallbacks_and_shows_the_counter(self):
        self.person(first_name="Anna")
        utskick = self.utskick()
        response = self.client.post(
            self.step(utskick, "innehall"),
            {
                "sms_body": BODY.replace("{förnamn|du}", "{förnamn}"),
                "reserv_förnamn": "du",
                "nasta": "tid",
            },
        )
        self.assertRedirects(response, self.step(utskick, "tid"))
        utskick.refresh_from_db()
        self.assertEqual(utskick.merge_fallbacks, {"förnamn": "du"})
        page = self.client.get(self.step(utskick, "innehall"))
        self.assertContains(page, "GSM-7 · ")
        self.assertContains(page, "Förhandsvisning med Anna")
        self.assertContains(page, links.sms_link(composer.SAMPLE_CODE))
        self.assertContains(page, "Svara STOPP för att inte få fler sms.")
        self.assertContains(page, 'data-ut-sms-count="ut-sms-count"')

    def test_template_fix_and_links(self):
        utskick = self.utskick(body="Hej, välkommen", link=False)
        url = self.step(utskick, "innehall")
        self.client.post(url, {"sms_body": "x", "mall": "oppettider"})
        utskick.refresh_from_db()
        self.assertIn("Exempelrör har nya öppettider", utskick.sms_body)
        self.assertEqual((utskick.purpose, utskick.info_reason), (INFORMATION, "oppettider"))
        curly = "Hej " + chr(0x2019) + "du" + chr(0x2019) + " " + chr(0x2013) + " boka"
        self.client.post(url, {"sms_body": curly, "action": "fix"})
        utskick.refresh_from_db()
        self.assertEqual(utskick.sms_body, "Hej 'du' - boka")
        self.assertEqual(encoding.analyse(utskick.sms_body).encoding, encoding.GSM7)
        response = self.client.post(
            url,
            {
                "sms_body": "Hej Exempelrör",
                "action": "lank",
                "lank_nyckel": "Boka tid",
                "lank_adress": "https://okand-sajt.example/boka?gclid=abc",
            },
        )
        self.assertRedirects(response, url + "#ut-lankar", fetch_redirect_response=False)
        link = TrackedLink.objects.get(utskick=utskick)
        self.assertEqual(link.key, "boka-tid")
        self.assertNotIn("gclid", link.destination)
        utskick.refresh_from_db()
        self.assertEqual(utskick.sms_body, "Hej Exempelrör {länk:boka-tid}")
        host = AllowedHost.objects.get(account=self.account)
        self.assertEqual(host.status, AllowedHost.Status.PENDING)
        page = self.client.get(url)
        self.assertContains(page, "Väntar på ADX: länkar till nya webbplatser godkänns av ADX.")
        self.client.post(url, {"sms_body": utskick.sms_body, "ta_bort_lank": link.pk})
        self.assertFalse(TrackedLink.objects.filter(utskick=utskick).exists())

    def test_time_step(self):
        utskick = self.utskick()
        url = self.step(utskick, "tid")
        past = timezone.localtime(timezone.now() - timedelta(days=1))
        response = self.client.post(
            url, {"nar": "at", "datum": past.date().isoformat(), "klockan": "10:00"}
        )
        self.assertContains(response, "Välj en tid som inte har passerat.")
        later = timezone.localtime(timezone.now() + timedelta(days=3))
        response = self.client.post(
            url, {"nar": "at", "datum": later.date().isoformat(), "klockan": "10:00"}
        )
        self.assertRedirects(response, self.step(utskick, "granska"))
        utskick.refresh_from_db()
        self.assertEqual(utskick.send_mode, Utskick.SendMode.AT)
        self.assertEqual(timezone.localtime(utskick.scheduled_at).hour, 10)
        page = self.client.get(url)
        self.assertContains(page, "tidsfönstret")
        self.assertContains(page, "Högst 2 reklam-sms per kontakt och vecka.")

    def test_editing_a_scheduled_utskick_makes_it_a_draft_again(self):
        self.person()
        utskick = self.utskick(
            status=Utskick.Status.SCHEDULED,
            send_mode=Utskick.SendMode.AT,
            scheduled_at=timezone.now() + timedelta(days=2),
            confirmed_at=timezone.now(),
            confirm_summary={"sms": 1},
        )
        response = self.client.post(
            self.step(utskick, "innehall"), {"sms_body": BODY + " Välkommen.", "nasta": "tid"}
        )
        self.assertEqual(response.status_code, 302)
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.DRAFT)
        self.assertIsNone(utskick.confirmed_at)
        self.assertEqual(utskick.confirm_summary, {})


# ---------------------------------------------------------------------------
# Granska och bekräftelsen
# ---------------------------------------------------------------------------


class ReviewTests(UiFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.people = [self.person(), self.person()]
        self.utskick_row = self.utskick(send_mode=Utskick.SendMode.NOW)

    def items(self, response):
        return {(item["level"], item["text"]) for item in response.context["review"]["items"]}

    def test_a_clean_utskick_has_no_blocking_item(self):
        response = self.client.get(self.step(self.utskick_row, "granska"))
        review = response.context["review"]
        self.assertFalse(review["blocking"], review["items"])
        self.assertIn(("info", "2 får sms."), self.items(response))
        self.assertIn(("ok", "Svara STOPP läggs till sist i sms:et."), self.items(response))
        self.assertContains(response, "2 sms skickas nu. Kostnad cirka")
        self.assertContains(response, 'data-ut-dialog="ut-dialog-skicka"')
        self.assertContains(response, "<dialog")
        self.assertEqual(review["summary"]["sms"], 2)

    def test_blocking_items_disable_the_button(self):
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(sms_enabled=False)
        self.utskick_row.sms_body = "Hej {okänd}, besök www.example.com"
        self.utskick_row.save()
        response = self.client.get(self.step(self.utskick_row, "granska"))
        blocks = {text for level, text in self.items(response) if level == "block"}
        self.assertIn("Sms-utskick är inte påslagna än.", blocks)
        self.assertIn("Mottagaren ser bara ett nummer. Skriv Exempelrör i texten.", blocks)
        self.assertTrue(any("ingen platshållare" in text for text in blocks))
        self.assertTrue(any("Skriv inte adresser direkt" in text for text in blocks))
        self.assertTrue(response.context["review"]["blocking"])
        self.assertContains(response, " disabled")
        nonce = response.context["nonce"]
        response = self.client.post(
            self.url("flamingo:app_utskick_confirm", self.utskick_row), {"nonce": nonce}
        )
        self.assertRedirects(
            response, self.step(self.utskick_row, "granska"), fetch_redirect_response=False
        )
        self.utskick_row.refresh_from_db()
        self.assertEqual(self.utskick_row.status, Utskick.Status.DRAFT)

    def test_information_rules_block(self):
        self.utskick_row.purpose = INFORMATION
        self.utskick_row.info_reason = "oppettider"
        self.utskick_row.sms_body = "Exempelrör har 20 % rabatt i veckan."
        self.utskick_row.save()
        response = self.client.get(self.step(self.utskick_row, "granska"))
        self.assertIn(
            ("block", "Det här ser ut som reklam. Välj Reklam eller ta bort erbjudandet."),
            self.items(response),
        )

    def test_confirm_needs_the_current_nonce(self):
        self.granska_nonce(self.utskick_row)
        response = self.client.post(
            self.url("flamingo:app_utskick_confirm", self.utskick_row), {"nonce": "fel"}
        )
        self.assertRedirects(
            response, self.step(self.utskick_row, "granska"), fetch_redirect_response=False
        )
        self.utskick_row.refresh_from_db()
        self.assertEqual(self.utskick_row.status, Utskick.Status.DRAFT)

    def test_the_customer_confirms_and_the_engine_takes_over(self):
        nonce = self.granska_nonce(self.utskick_row)
        response = self.client.post(
            self.url("flamingo:app_utskick_confirm", self.utskick_row), {"nonce": nonce}
        )
        self.assertRedirects(response, self.url("flamingo:app_utskick", self.utskick_row))
        self.utskick_row.refresh_from_db()
        self.assertEqual(self.utskick_row.status, Utskick.Status.SCHEDULED)
        self.assertEqual(self.utskick_row.confirmed_by, self.anna)
        self.assertFalse(self.utskick_row.confirmed_as_staff)
        self.assertEqual(self.utskick_row.confirm_summary["sms"], 2)
        self.assertTrue(self.utskick_row.confirm_summary["send_now"])
        # Inget skickas i förfrågan: inga mottagare och inga sms ännu.
        self.assertFalse(Recipient.objects.filter(utskick=self.utskick_row).exists())
        self.assertFalse(SmsMessage.objects.exists())
        # Samma nonce en gång till nekas.
        again = self.client.post(
            self.url("flamingo:app_utskick_confirm", self.utskick_row), {"nonce": nonce}
        )
        self.assertEqual(again.status_code, 302)

    def test_staff_in_view_as_must_tick_the_box_and_is_logged(self):
        staff = self.client_for(self.staff)
        nonce = self.granska_nonce(self.utskick_row, staff)
        page = staff.get(self.step(self.utskick_row, "granska"))
        self.assertContains(page, "Jag skickar det här som ADX åt Exempelrör.")
        self.assertContains(page, "Skicka som ADX")
        nonce = page.context["nonce"]
        response = staff.post(
            self.url("flamingo:app_utskick_confirm", self.utskick_row), {"nonce": nonce}
        )
        self.assertRedirects(
            response, self.step(self.utskick_row, "granska"), fetch_redirect_response=False
        )
        self.utskick_row.refresh_from_db()
        self.assertEqual(self.utskick_row.status, Utskick.Status.DRAFT)
        nonce = self.granska_nonce(self.utskick_row, staff)
        staff.post(
            self.url("flamingo:app_utskick_confirm", self.utskick_row),
            {"nonce": nonce, "som_adx": "1"},
        )
        self.utskick_row.refresh_from_db()
        self.assertEqual(self.utskick_row.status, Utskick.Status.SCHEDULED)
        self.assertTrue(self.utskick_row.confirmed_as_staff)
        self.assertEqual(self.utskick_row.confirmed_by, self.staff)
        report = staff.get(self.url("flamingo:app_utskick", self.utskick_row))
        self.assertContains(report, "Skickat av ADX")

    def test_the_link_check_says_only_whether_links_answer(self):
        url = self.url("flamingo:app_utskick_link_check", self.utskick_row)
        target = "apps.utskick.links.check_destinations"
        answer = {"https://exempelror.example/boka": True}
        with mock.patch(target, return_value=answer):
            data = self.client.post(url, HTTP_ACCEPT="application/json").json()
        self.assertEqual(data["text"], "1 av 1 länkar svarar.")
        with mock.patch(target, return_value={}):
            data = self.client.post(url, HTTP_ACCEPT="application/json").json()
        self.assertIn("kunde inte kontrolleras", data["text"])
        with mock.patch(target, return_value=answer):
            response = self.client.post(url)
        self.assertRedirects(response, self.step(self.utskick_row, "granska"))

    def test_the_cost_cap_blocks_send_now(self):
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(monthly_cap_kr=0)
        response = self.client.get(self.step(self.utskick_row, "granska"))
        blocks = [text for level, text in self.items(response) if level == "block"]
        self.assertTrue(any(text.startswith("Ryms inte i taket") for text in blocks), blocks)

    def test_the_demo_is_told_it_never_sends(self):
        type(self.account).objects.filter(pk=self.account.pk).update(is_demo=True)
        response = self.client.get(self.step(self.utskick_row, "granska"))
        self.assertIn(
            ("info", "Demokontot skickar aldrig. Utskicket visas som skickat."),
            self.items(response),
        )


# ---------------------------------------------------------------------------
# Läget (I.5)
# ---------------------------------------------------------------------------


class StateTests(UiFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.person()
        self.row = self.utskick(
            status=Utskick.Status.SENDING,
            started_at=timezone.now(),
            frozen_at=timezone.now(),
            confirm_summary={"sms": 1},
        )
        self.state_url = self.url("flamingo:app_utskick_state", self.row)

    def test_pause_resume_and_cancel(self):
        self.client.post(self.state_url, {"action": "pausa"})
        self.row.refresh_from_db()
        self.assertEqual((self.row.status, self.row.pause_reason), ("paused", "customer"))
        report = self.client.get(self.url("flamingo:app_utskick", self.row))
        self.assertContains(report, "Du har pausat utskicket.")
        self.assertContains(report, "Fortsätt")
        self.client.post(self.state_url, {"action": "fortsatt"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.SENDING)
        self.client.post(self.state_url, {"action": "pausa"})
        self.client.post(self.state_url, {"action": "avbryt"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.CANCELLED)

    def test_staff_pause_is_released_by_staff_only_with_the_box(self):
        staff = self.client_for(self.staff)
        staff.post(self.state_url, {"action": "pausa"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.pause_reason, Utskick.PauseReason.STAFF)
        self.client.post(self.state_url, {"action": "fortsatt"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.PAUSED)
        staff.post(self.state_url, {"action": "fortsatt"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.PAUSED)
        staff.post(self.state_url, {"action": "fortsatt", "som_adx": "1"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.SENDING)

    def test_a_reconfirm_pause_goes_to_review(self):
        Utskick.objects.filter(pk=self.row.pk).update(
            status=Utskick.Status.PAUSED,
            pause_reason=Utskick.PauseReason.LATE,
            scheduled_at=timezone.now() - timedelta(hours=5),
        )
        report = self.client.get(self.url("flamingo:app_utskick", self.row))
        self.assertContains(report, "Granska igen")
        self.assertContains(report, "men kunde inte skickas då")
        response = self.client.post(self.state_url, {"action": "fortsatt"})
        self.assertRedirects(response, self.step(self.row, "granska"))
        self.assertEqual(self.client.get(self.step(self.row, "granska")).status_code, 200)
        self.assertEqual(self.client.get(self.step(self.row, "innehall")).status_code, 302)

    @override_settings(INQUIRY_NOTIFICATION_EMAIL="byran@adx.example")
    def test_ask_adx_alerts_the_agency_once_and_never_mails_the_customer(self):
        Utskick.objects.filter(pk=self.row.pk).update(
            status=Utskick.Status.PAUSED_CAP, pause_reason=Utskick.PauseReason.SMS_COST_CAP
        )
        report = self.client.get(self.url("flamingo:app_utskick", self.row))
        self.assertContains(report, "Pausat vid taket:")
        self.assertContains(report, "Be ADX höja taket")
        self.client.post(self.state_url, {"action": "be_om_tak"})
        self.client.post(self.state_url, {"action": "be_om_tak"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["byran@adx.example"])

    def test_unknown_action_is_400(self):
        self.assertEqual(self.client.post(self.state_url, {"action": "x"}).status_code, 400)


# ---------------------------------------------------------------------------
# Rapporten och mottagarna (I.8)
# ---------------------------------------------------------------------------


class ReportTests(UiFixture, TestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.row = self.utskick(
            status=Utskick.Status.SENT, started_at=now, finished_at=now, frozen_at=now
        )
        people = [self.person() for _ in range(4)]
        S = Recipient.Status
        message = SmsMessage.objects.create(
            account=self.sms_account,
            source=SmsMessage.Source.UTSKICK,
            to=people[0].phone,
            body="x",
            parts=1,
            status=SmsMessage.Status.DELIVERED,
            customer_price=5700,
        )
        self.delivered = Recipient.objects.create(
            utskick=self.row,
            contact=people[0],
            channel=CHANNEL_SMS,
            address=people[0].phone,
            status=S.DELIVERED,
            sent_at=now,
            delivered_at=now,
            sms_message=message,
            first_clicked_at=now,
            click_count=2,
            replied_at=now,
        )
        Recipient.objects.create(
            utskick=self.row,
            contact=people[1],
            channel=CHANNEL_SMS,
            address=people[1].phone,
            status=S.DELIVERED,
            sent_at=now,
            delivered_at=now,
            stopped_at=now,
        )
        Recipient.objects.create(
            utskick=self.row,
            contact=people[2],
            channel=CHANNEL_SMS,
            address=people[2].phone,
            status=S.FAILED,
            sent_at=now,
        )
        Recipient.objects.create(
            utskick=self.row,
            contact=people[3],
            channel=CHANNEL_SMS,
            address=people[3].phone,
            status=S.SKIPPED,
            skip_reason=Recipient.SkipReason.WEEKLY_CAP,
        )
        Click.objects.create(
            account=self.account,
            utskick=self.row,
            recipient=self.delivered,
            contact=people[0],
            channel="sms",
            engaged_seconds=112,
        )
        Lead.objects.create(
            account=self.account, utskick=self.row, utskick_recipient=self.delivered, name="Anna"
        )
        Lead.objects.create(
            account=self.account, utskick=self.row, name="Sen", attribution={"late": True}
        )
        Lead.objects.create(account=self.account, utskick=self.row, source=Lead.SOURCE_REPLY)

    def test_summary_numbers(self):
        numbers = reports.summary(self.row)
        expected = {
            "total": 3,
            "skipped": 1,
            "sent": 2,
            "delivered": 2,
            "failed": 1,
            "clicked": 1,
            "clicks": 2,
            "engaged": 1,
            "replied": 1,
            "stopped": 1,
            "leads": 1,
            "leads_late": 1,
            "cost_units": 5700,
        }
        for key, value in expected.items():
            self.assertEqual(numbers[key], value, key)
        self.assertEqual(numbers["skipped_by_reason"], {"weekly_cap": 1})
        self.assertEqual(numbers["click_pct"], 50.0)

    def test_report_page_and_tiles_link_to_the_recipients(self):
        response = self.client.get(self.url("flamingo:app_utskick", self.row))
        self.assertContains(response, "Levererade")
        self.assertContains(response, "1 STOPP")
        self.assertContains(response, "+1 senare")
        self.assertContains(response, "Veckotaket")
        self.assertContains(response, "?visa=hoppades-over-weekly_cap")
        self.assertContains(response, "1 min 52 s")
        self.assertContains(response, "kostnad 1")

    def test_recipient_views(self):
        url = self.url("flamingo:app_utskick_recipients", self.row)
        for view, count in (
            ("alla", 3),
            ("levererade", 2),
            ("klickade", 1),
            ("svarade", 1),
            ("stopp", 1),
            ("forfragan", 1),
            ("hoppades-over", 1),
            ("hoppades-over-weekly_cap", 1),
            ("stannade", 1),
            ("misslyckade", 1),
        ):
            with self.subTest(view=view):
                response = self.client.get(url, {"visa": view})
                self.assertEqual(response.context["page"].paginator.count, count)
        self.assertEqual(self.client.get(url, {"visa": "okand"}).context["view"], "alla")

    def test_save_as_a_new_or_existing_list(self):
        url = self.url("flamingo:app_utskick_save_list", self.row)
        self.client.post(url, {"visa": "klickade", "lista_id": "ny", "ny_lista": "Klickade"})
        lista = ContactList.objects.get(account=self.account, name="Klickade")
        self.assertEqual(lista.memberships.count(), 1)
        self.assertEqual(lista.memberships.get().source, ListMembership.Source.REPORT)
        self.client.post(url, {"visa": "levererade", "lista_id": lista.pk})
        self.assertEqual(lista.memberships.count(), 2)

    def test_final_stats_survive_retention(self):
        stats = reports.final_stats(self.row)
        self.assertEqual(stats["delivered"], 2)
        self.assertNotIn("address", str(stats))
        Utskick.objects.filter(pk=self.row.pk).update(stats=stats)
        Recipient.objects.filter(utskick=self.row).delete()
        self.row.refresh_from_db()
        numbers = reports.summary(self.row)
        self.assertTrue(numbers["from_stats"])
        self.assertEqual(numbers["delivered"], 2)
        response = self.client.get(self.url("flamingo:app_utskick", self.row))
        self.assertContains(response, "Siffrorna är sparade när utskicket blev klart.")

    def test_list_columns_and_status_labels(self):
        self.utskick(status=Utskick.Status.DRAFT, link=False)
        Utskick.objects.filter(status=Utskick.Status.DRAFT).update(name="Ett utkast")
        response = self.client.get(self.url("flamingo:app_utskick_list"))
        self.assertContains(response, "Utkast")
        self.assertContains(response, "Skickat ")
        self.assertContains(response, "Lista Kunder")
        self.assertContains(response, 'data-label="Förfrågningar"')
        response = self.client.get(self.url("flamingo:app_utskick_list"), {"visa": "utkast"})
        self.assertContains(response, "Ett utkast")
        self.assertNotContains(response, "Skickat ")

    def test_failed_has_a_chip_and_each_view_its_own_empty_text(self):
        """UX-granskningen: "gick inte fram" hade ingen sida att leda till,
        och sidan utan rader läste konstigt."""
        url = self.url("flamingo:app_utskick_recipients", self.row)
        response = self.client.get(url)
        self.assertContains(response, "?visa=misslyckade")
        self.assertContains(response, "Gick inte fram")
        Recipient.objects.filter(utskick=self.row).delete()
        for view, text in (
            ("forfragan", "Ingen har skickat en förfrågan än."),
            ("misslyckade", "Inget sms har misslyckats."),
            ("hoppades-over-weekly_cap", "Ingen hoppades över."),
        ):
            with self.subTest(view=view):
                self.assertContains(self.client.get(url, {"visa": view}), text)

    def test_empty_list(self):
        Utskick.objects.all().delete()
        response = self.client.get(self.url("flamingo:app_utskick_list"))
        self.assertContains(response, "Inga utskick än.")
        self.assertContains(response, reverse("flamingo:app_utskick_new"))


# ---------------------------------------------------------------------------
# Testsms (F.8)
# ---------------------------------------------------------------------------


@LIVE
class TestSendTests(UiFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeElks()
        patcher = mock.patch("apps.sms.elks._post", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        type(self.account).objects.filter(pk=self.account.pk).update(notify_phone="070-174 09 99")
        self.account.refresh_from_db()
        self.kontakt = self.person(first_name="Greta")
        self.row = self.utskick()
        self.test_url = self.url("flamingo:app_utskick_test", self.row)

    def test_a_customer_sends_a_test_to_the_number_for_inquiries(self):
        response = self.client.post(self.test_url, {"till": "agare", "kontakt": self.kontakt.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.fake.sends), 1)
        sent = self.fake.sends[0]
        self.assertEqual(sent["to"], "+46701740999")
        self.assertEqual(sent["from"], REPLY)
        self.assertIn("Hej Greta", sent["message"])
        self.assertRegex(
            sent["message"], r"k\.localhost:8770/[A-Za-z0-9]{6}|k\.adx\.se/[A-Za-z0-9]{6}"
        )
        self.assertTrue(sent["message"].endswith("Svara STOPP för att inte få fler sms."))
        message = SmsMessage.objects.get()
        self.assertEqual(message.source, SmsMessage.Source.TEST)
        code = LinkCode.objects.get(kind=LinkCode.Kind.LINK)
        self.assertIsNone(code.recipient_id)
        self.assertEqual(code.link.utskick, self.row)

    def test_the_content_step_saves_first_and_then_tests(self):
        response = self.client.post(
            self.step(self.row, "innehall"),
            {"sms_body": "Hej {förnamn}, Exempelrör testar.", "till": "agare"},
        )
        self.assertRedirects(
            response, self.step(self.row, "innehall") + "#ut-test", fetch_redirect_response=False
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.sms_body, "Hej {förnamn}, Exempelrör testar.")
        self.assertEqual(len(self.fake.sends), 1)

    def test_refusals(self):
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(sms_enabled=False)
        self.client.post(self.test_url, {"till": "agare"})
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(sms_enabled=True)
        self.row.sms_body = "Hej, boka: {länk:boka}"
        self.row.save()
        self.client.post(self.test_url, {"till": "agare"})
        self.row.sms_body = BODY
        self.row.save()
        # En kund kan inte skriva ett eget nummer.
        self.client.post(self.test_url, {"till": "eget", "nummer": "0701740998"})
        self.assertEqual(self.fake.sends, [])

    def test_ten_a_day(self):
        for _ in range(views.TEST_SENDS_PER_DAY + 2):
            self.client.post(self.test_url, {"till": "agare"})
        self.assertEqual(len(self.fake.sends), views.TEST_SENDS_PER_DAY)

    def test_the_demo_never_sends(self):
        type(self.account).objects.filter(pk=self.account.pk).update(is_demo=True)
        response = self.client.post(self.test_url, {"till": "agare"}, follow=True)
        self.assertContains(response, "Demokontot skickar aldrig.")
        self.assertEqual(self.fake.calls, [])

    def test_staff_sends_to_their_own_number_and_needs_the_box_for_the_customer(self):
        staff = self.client_for(self.staff)
        staff.post(self.test_url, {"till": "agare"})
        self.assertEqual(self.fake.sends, [])
        staff.post(self.test_url, {"till": "agare", "som_adx": "1"})
        self.assertEqual(len(self.fake.sends), 1)
        staff.post(self.test_url, {"till": "eget", "nummer": "070-174 09 98"})
        self.assertEqual(self.fake.sends[-1]["to"], "+46701740998")

    def test_a_suppressed_contact_gets_no_test_and_a_contact_gets_an_event(self):
        kontakt = self.person(phone="+46701740999")
        self.client.post(self.test_url, {"till": "agare"})
        self.assertEqual(len(self.fake.sends), 1)
        event = Event.objects.get(contact=kontakt, kind=Event.TEST_SEND)
        # Vem som skickade testet sparas (I.4).
        self.assertEqual(event.data, {"utskick": self.row.pk, "user": self.anna.pk, "staff": False})
        consents.set_status(kontakt, CHANNEL_SMS, consents.UNSUBSCRIBED, source=Consent.Source.STOP)
        self.client.post(self.test_url, {"till": "agare"})
        self.assertEqual(len(self.fake.sends), 1)


# ---------------------------------------------------------------------------
# Inställningar (I.9)
# ---------------------------------------------------------------------------


class SettingsTests(UiFixture, TestCase):
    def test_the_s2_rows(self):
        response = self.client.get(self.url("flamingo:app_utskick_settings"))
        self.assertContains(response, "0766 86 00 46 · delas av alla ADX-kunder")
        self.assertContains(response, "Exempelror · godkända av ADX")
        self.assertContains(response, "Be ADX ändra taket")
        self.assertContains(response, "gemensamt med sms-API:t")
        self.assertEqual(response.context["ut_nav"]["current_label"], "Inställningar")

    def test_window_caps_and_notice_are_saved_within_bounds(self):
        url = self.url("flamingo:app_utskick_settings")
        data = {
            "weekday_start": "8",
            "weekday_end": "21",
            "weekend_start": "11",
            "weekend_end": "16",
            "weekly_cap_sms": "3",
            "weekly_cap_email": "5",
        }
        self.assertRedirects(self.client.post(url, data), url)
        row = UtskickSettings.objects.get(account=self.account)
        self.assertEqual(row.sms_window, {"weekday": [8, 21], "weekend": [11, 16]})
        self.assertEqual((row.weekly_cap_sms, row.weekly_cap_email), (3, 5))
        self.assertFalse(row.notify_on_reply)
        response = self.client.post(url, {**data, "weekday_start": "7", "weekly_cap_sms": "9"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("weekday", response.context["errors"])
        self.assertIn("weekly_cap_sms", response.context["errors"])
        response = self.client.post(url, {**data, "weekend_start": "16"})
        self.assertIn("weekend", response.context["errors"])
        other = UtskickSettings.objects.get(account=self.other_account)
        self.assertEqual(other.weekly_cap_sms, 2)

    def test_the_email_cap_is_hidden_until_email_is_live(self):
        """UX-granskningen: ingen obyggd funktion syns i produkten. Det sparade
        värdet följer med dolt, så att en sparning inte ändrar det."""
        url = self.url("flamingo:app_utskick_settings")
        UtskickSettings.objects.filter(account=self.account).update(weekly_cap_email=5)
        page = self.client.get(url)
        self.assertNotContains(page, "Reklammejl per vecka")
        self.assertContains(page, '<input type="hidden" name="weekly_cap_email" value="5">')
        with override_settings(UTSKICK_EMAIL_LIVE=True):
            Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(email_enabled=True)
            self.assertContains(self.client.get(url), "Reklammejl per vecka")


# ---------------------------------------------------------------------------
# Sms-texten (composer.py)
# ---------------------------------------------------------------------------


class ComposerTests(UiFixture, TestCase):
    def test_placeholders_and_validation(self):
        found = composer.placeholders(
            "Hej {Förnamn|du} {fält:regnr} {länk:boka} {x} {avregistrering}"
        )
        self.assertEqual(found.tags, ["förnamn", "fält:regnr"])
        self.assertEqual(found.links, ["boka"])
        self.assertEqual(found.unsubscribe, 1)
        self.assertEqual(found.unknown, ["{x}"])
        utskick = self.utskick(body="x", link=False)
        errors = composer.validate(
            self.account, "Hej {fält:regnr} {länk:boka} https://a.example", utskick
        )
        self.assertEqual(len(errors), 3)
        FieldDef.objects.create(account=self.account, key="regnr", label="Regnr")
        TrackedLink.objects.create(
            account=self.account,
            utskick=utskick,
            kind="external",
            key="boka",
            destination="https://exempelror.example/",
        )
        self.assertEqual(
            composer.validate(self.account, "Hej {fält:regnr} {länk:boka}", utskick), []
        )
        # Samma text i redigeraren och i Granska, där det inte finns något
        # "nedanför" (UX-granskningen).
        (missing,) = composer.validate(self.account, "Hej {länk:annan}", utskick)
        self.assertEqual(
            missing, "Länken {länk:annan} finns inte. Lägg till den under Länkar i Innehåll."
        )

    def test_expensive_characters_have_names(self):
        self.assertEqual(composer.non_gsm_text([]), "")
        self.assertEqual(
            composer.non_gsm_text([chr(0xA0), chr(0x2009), chr(0xFEFF), chr(0x2014), "😀"]),
            "ett hårt mellanslag, ett smalt mellanslag, ett osynligt tecken, "
            f"{chr(0x2014)} (ett tankstreck), 😀",
        )
        self.assertEqual(composer.validate(self.account, "  "), ["Skriv texten i sms:et."])

    def test_merge_values_are_one_line_and_capped(self):
        values = {"förnamn": "Anna\nLisa", "efternamn": "x" * 80}
        text = composer.merge("{förnamn} {efternamn} {företag|ni} {namn}", values, {"namn": "kund"})
        self.assertEqual(text, "Anna Lisa " + "x" * 60 + " ni kund")

    def test_render_with_codes_for_both_senders(self):
        kontakt = self.person(first_name="Anna")
        utskick = self.utskick()
        recipient = Recipient.objects.create(
            utskick=utskick,
            contact=kontakt,
            channel=CHANNEL_SMS,
            address=kontakt.phone,
            merge=composer.merge_values(kontakt),
        )
        text = composer.render_sms(utskick, recipient, REPLY)
        code = LinkCode.objects.get(recipient=recipient, kind=LinkCode.Kind.LINK).code
        self.assertIn("Hej Anna,", text)
        self.assertIn(f"/{code}", text)
        self.assertTrue(text.endswith("\nSvara STOPP för att inte få fler sms."))
        # Samma kod nästa gång (ingen ny rad).
        composer.render_sms(utskick, recipient, REPLY)
        self.assertEqual(LinkCode.objects.filter(recipient=recipient).count(), 1)
        utskick.sms_body = BODY + " Svara STOPP för att inte få fler sms."
        named = composer.render_sms(utskick, recipient, "Exempelror")
        person = LinkCode.objects.get(recipient=recipient, kind=LinkCode.Kind.PERSON).code
        self.assertNotIn("Svara STOPP", named)
        self.assertTrue(
            named.endswith(f"Avregistrera: k.localhost:8770/s/{person}")
            or named.endswith(f"/s/{person}")
        )
        self.assertEqual(encoding.analyse(named).encoding, encoding.GSM7)
        utskick.sms_body = "Hej {avregistrering} hej då, Exempelrör"
        self.assertEqual(
            composer.render_sms(utskick, recipient, REPLY),
            "Hej Svara STOPP för att inte få fler sms. hej då, Exempelrör",
        )

    def test_preview_counts_the_longest_names(self):
        utskick = self.utskick(body="Hej {förnamn}, " + "a" * 100 + " Exempelrör", link=False)
        self.person(first_name="Bo")
        self.person(first_name="B" * 40)
        shown = composer.preview(utskick)
        self.assertEqual(shown["parts"], 1)
        self.assertEqual((shown["longest_parts"], shown["longest_count"]), (2, 1))
        self.assertTrue(shown["counter"].startswith("GSM-7 · "))
        self.assertGreater(shown["cost_ore"], 0)
        shown = composer.preview(utskick, body="Hej " + chr(0x2019) + "du" + chr(0x2019))
        self.assertEqual(shown["encoding"], encoding.UCS2)
        self.assertEqual(shown["non_gsm"], [chr(0x2019)])

    def test_the_built_in_templates_pass_the_guards(self):
        for template in composer.TEMPLATES:
            text = composer.template_body(template["key"], "Exempelrör")
            with self.subTest(template=template["key"]):
                self.assertIn("Exempelrör", text)
                self.assertNotRegex(text, r"\bD\b")
                self.assertIsNone(URGENCY.search(text))
                self.assertIsNone(SPEED.search(text))
                for phrase in PromiseGuardTests.PHRASES:
                    self.assertNotIn(phrase, text.lower())
                for code in (0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026):
                    self.assertNotIn(chr(code), text)
                self.assertNotIn(chr(0x21), text)
                self.assertEqual(encoding.analyse(text).encoding, encoding.GSM7)


# ---------------------------------------------------------------------------
# Vakter: 375 px, räknarens kontrakt och mallarna
# ---------------------------------------------------------------------------


class GuardTests(TestCase):
    css = (BASE / "static" / "css" / "flamingo-app-utskick-views.css").read_text("utf-8")
    js = (BASE / "static" / "js" / "flamingo-app-utskick.js").read_text("utf-8")

    def test_every_view_template_exists_and_extends_the_layout(self):
        for name in (
            "list",
            "step_mottagare",
            "step_kanal",
            "step_innehall",
            "step_tid",
            "step_review",
            "report",
            "recipients",
            "settings",
        ):
            with self.subTest(template=name):
                text = (TEMPLATES / f"{name}.html").read_text("utf-8")
                self.assertTrue(
                    text.startswith('{% extends "flamingo/app/utskick/_layout.html" %}')
                )
        self.assertFalse((TEMPLATES / "_stub.html").exists())

    def test_tables_have_labels_and_a_header_row(self):
        for path in TEMPLATES.glob("*.html"):
            text = path.read_text("utf-8")
            for table in re.findall(r"<table\b.*?</table>", text, flags=re.S):
                with self.subTest(path=path.name):
                    self.assertIn("<thead>", table)
                    self.assertIn("fl-table", table)

    def test_phone_widths(self):
        """375 px (README I.2): kolumnerna blir en under 900 px, stegchipsen
        blir "Steg 3 av 5" och knapparna breda under 560 px, dialogen och
        telefonen ryms alltid."""
        self.assertIn("width:min(100vw - 32px,480px)", self.css)
        self.assertIn("max-width:100%", self.css[self.css.index(".fl-ut-phone{") :])
        narrow = self.css[self.css.index("@media (max-width:560px)") :]
        self.assertIn(".fl-ut-steps{display:none}", narrow)
        self.assertIn(".fl-ut-steps__compact{display:block}", narrow)
        self.assertIn(".fl-ut-actions .fl-btn{width:100%}", narrow)
        medium = self.css[self.css.index("@media (max-width:900px)") :]
        self.assertIn(
            ".fl-ut-cols,.fl-ut-editor,.fl-ut-review{grid-template-columns:minmax(0,1fr)}", medium
        )

    def test_the_counter_mirrors_apps_sms_encoding(self):
        """Räknarens kontrakt (S2-HANDOFF.md): samma GSM-tabeller och delar
        som apps/sms/encoding.py, och attributen svarstråden använder."""
        basic = re.search(r"var GSM_BASIC =\s*((?:\"(?:[^\"\\]|\\.)*\"\s*\+?\s*)+);", self.js)
        pieces = re.findall(r"\"((?:[^\"\\]|\\.)*)\"", basic.group(1))
        self.assertEqual("".join(ast.literal_eval(f'"{p}"') for p in pieces), encoding._GSM_BASIC)
        extended = re.search(r"var GSM_EXTENDED = \"((?:[^\"\\]|\\.)*)\";", self.js).group(1)
        self.assertEqual(ast.literal_eval(f'"{extended}"'), encoding._GSM_EXTENDED)
        for needle in (
            "textarea[data-ut-sms]",
            "data-ut-sms-count",
            "data-ut-sms-suffix",
            "data-ut-sms-ore-per-part",
            "single = gsm ? 160 : 70",
            "perPart = gsm ? 153 : 67",
        ):
            self.assertIn(needle, self.js)

    def test_review_findings_in_the_templates(self):
        """UX-granskningen: räknaren läses inte upp vid varje tangent (bara
        anteckningarna), tråden skriver klockan 09.00, och kundens egen
        tråd talar inte om "kunden"."""
        editor = (TEMPLATES / "step_innehall.html").read_text("utf-8")
        self.assertNotRegex(editor, r'id="ut-sms-count"[^>]*aria-live')
        self.assertRegex(editor, r'id="ut-sms-notes"[^>]*aria-live="polite"')
        self.assertIn("data-ut-sms-gsm", self.js)
        self.assertIn("form[data-ut-guard]", self.js)
        thread = (TEMPLATES / "_thread.html").read_text("utf-8")
        self.assertIn('date:"j b H.i"', thread)
        self.assertNotIn('date:"j b H:i"', thread)
        from . import threads

        self.assertNotIn("kunden", " ".join(threads.ANSWER_TEXTS.values()))


# ---------------------------------------------------------------------------
# Utanför utskicksvyerna: översikten, tre saker, kontaktkortet (C.2, I.5, I.7)
# ---------------------------------------------------------------------------


class OutsideTests(UiFixture, TestCase):
    def test_overview_counts_replies_apart_and_google_cost_only_on_google_leads(self):
        from apps.flamingo.app_views.overview import Numbers, numbers_for

        row = self.utskick(status=Utskick.Status.SENT)
        Lead.objects.create(account=self.account, name="Från Google")
        Lead.objects.create(account=self.account, name="Från utskick", utskick=row)
        Lead.objects.create(account=self.account, source=Lead.SOURCE_REPLY, utskick=row)
        numbers = numbers_for(self.account, timezone.now())
        self.assertEqual(numbers.leads, 2)
        self.assertEqual(numbers.google_leads, 1)
        self.assertEqual(numbers.utskick_leads, 1)
        priced = Numbers(
            leads=2,
            deals=0,
            deals_without_value=0,
            deal_value_kr=0,
            spend_kr=300,
            cohort_deals=0,
            google_leads=1,
        )
        self.assertEqual(priced.kr_per_lead, 300)
        page = self.client.get(reverse("flamingo:app"))
        self.assertContains(page, "Varav via utskick: 1 förfrågan, 0 affärer.")

    def test_three_things_name_paused_utskick_and_waiting_links_first(self):
        from apps.flamingo.rules import three_things

        row = self.utskick(
            status=Utskick.Status.PAUSED_CAP, pause_reason=Utskick.PauseReason.SMS_COST_CAP
        )
        AllowedHost.objects.create(account=self.account, host="okand.example")
        things = three_things(self.account)
        self.assertEqual(things[0].title, "Utskicket Höstservice värmepump")
        self.assertEqual(things[0].text, "är pausat vid taket.")
        self.assertEqual(things[0].url, reverse("flamingo:app_utskick", args=[row.pk]))
        self.assertIn("okand.example", things[1].text)
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        self.assertFalse([t for t in three_things(self.account) if t.key.startswith("utskick")])

    def test_the_contact_card_shows_sms_clicks_and_replies(self):
        from .models import Thread, ThreadMessage

        kontakt = self.person(first_name="Anna")
        row = self.utskick(status=Utskick.Status.SENT)
        now = timezone.now()
        recipient = Recipient.objects.create(
            utskick=row,
            contact=kontakt,
            channel=CHANNEL_SMS,
            address=kontakt.phone,
            status=Recipient.Status.DELIVERED,
            sent_at=now,
            delivered_at=now,
            click_count=2,
            first_clicked_at=now,
        )
        Click.objects.create(
            account=self.account,
            utskick=row,
            recipient=recipient,
            contact=kontakt,
            channel="sms",
            engaged_seconds=95,
            # Som klicket sparar den (analytics DeviceType), visas på svenska.
            device="mobile",
        )
        lead = Lead.objects.create(account=self.account, source=Lead.SOURCE_REPLY)
        thread = Thread.objects.create(
            account=self.account, contact=kontakt, channel="sms", lead=lead, utskick=row
        )
        ThreadMessage.objects.create(thread=thread, direction="in", body="Har ni tid tisdag?")
        page = self.client.get(reverse("flamingo:app_contact", args=[kontakt.pk]))
        self.assertContains(page, "Sms: Höstservice värmepump")
        self.assertContains(page, "Levererat")
        self.assertContains(page, "Klickade på länken")
        self.assertContains(page, "stannade 1 min 35 s på sidan · mobil")
        self.assertNotContains(page, "· mobile")
        self.assertContains(page, "Har ni tid tisdag?")
        self.assertContains(page, "1 utskick · 2 klick")


# ---------------------------------------------------------------------------
# Granskningarna av S2 (2026-10-10): ändringar mitt i sändningen, guiden,
# en ny bekräftelse, rapporten och texterna
# ---------------------------------------------------------------------------


class EditRaceTests(UiFixture, TestCase):
    """Sändningsgranskningen: en ändring kunde landa på ett utskick som
    ticken redan börjat frysa, och en länk som byttes gjorde inget
    schemalagt till ett utkast (säkerhetsgranskningen)."""

    def setUp(self):
        super().setUp()
        self.person()
        self.row = self.utskick(
            status=Utskick.Status.SCHEDULED,
            send_mode=Utskick.SendMode.AT,
            scheduled_at=timezone.now() + timedelta(days=2),
            confirmed_at=timezone.now(),
            confirm_summary={"sms": 1},
        )
        self.url_innehall = self.step(self.row, "innehall")

    def test_removing_or_adding_a_link_makes_a_scheduled_utskick_a_draft(self):
        link = TrackedLink.objects.get(utskick=self.row)
        self.client.post(
            self.url_innehall, {"sms_body": self.row.sms_body, "ta_bort_lank": link.pk}
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.DRAFT)
        self.assertIsNone(self.row.confirmed_at)
        Utskick.objects.filter(pk=self.row.pk).update(
            status=Utskick.Status.SCHEDULED, confirmed_at=timezone.now()
        )
        # Samma nyckel tillbaka med ett annat mål: texten står kvar, men det
        # är en ändring.
        self.client.post(
            self.url_innehall,
            {
                "sms_body": self.row.sms_body,
                "action": "lank",
                "lank_nyckel": "boka",
                "lank_adress": "https://exempelror.example/annan",
            },
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.DRAFT)
        self.assertEqual(self.row.sms_body, BODY)

    def test_a_change_after_the_tick_took_the_utskick_is_refused(self):
        stale = Utskick.objects.get(pk=self.row.pk)
        Utskick.objects.filter(pk=self.row.pk).update(status=Utskick.Status.FREEZING)
        link = TrackedLink.objects.get(utskick=self.row)
        with mock.patch.object(views, "owned", return_value=stale):
            response = self.client.post(
                self.url_innehall, {"sms_body": "Ny text från Exempelrör", "ta_bort_lank": link.pk}
            )
        self.assertRedirects(
            response, self.url("flamingo:app_utskick", self.row), fetch_redirect_response=False
        )
        self.row.refresh_from_db()
        self.assertEqual((self.row.status, self.row.sms_body), (Utskick.Status.FREEZING, BODY))
        self.assertTrue(TrackedLink.objects.filter(pk=link.pk).exists())
        with self.assertRaises(views.NotEditable):
            with views._editing(stale):
                pass
        for step, data in (
            ("mottagare", {"namn": "Annat namn", "lists": [self.kunder.pk]}),
            ("kanal", {"syfte": REKLAM, "avsandare": "name:Exempelror"}),
            ("tid", {"nar": "now"}),
        ):
            with self.subTest(step=step):
                with mock.patch.object(views, "owned", return_value=stale):
                    self.client.post(self.step(self.row, step), data)
                self.row.refresh_from_db()
                self.assertEqual(self.row.status, Utskick.Status.FREEZING)
                self.assertEqual(self.row.name, "Höstservice värmepump")
                self.assertEqual(self.row.send_mode, Utskick.SendMode.AT)


class GuideFlowTests(UiFixture, TestCase):
    """UX-granskningen: chipsen och "byt kontakt" tappade osparad text."""

    def setUp(self):
        super().setUp()
        self.kontakter = [self.person(first_name="Anna"), self.person(first_name="Bo")]
        self.row = self.utskick()

    def test_the_step_chips_save_the_step_first(self):
        page = self.client.get(self.step(self.row, "innehall")).content.decode()
        # Den dolda knappen som tar Enter står först, sedan "Utskick" och chipsen.
        first = page.index('form="ut-innehall"')
        self.assertIn('name="nasta" value="innehall"', page[first - 120 : first + 120])
        self.assertIn('form="ut-innehall" class="fl-ut-crumb" name="nasta" value="lista"', page)
        for key in ("mottagare", "kanal", "tid", "granska"):
            self.assertIn(
                f'form="ut-innehall" class="fl-ut-steps__link" name="nasta" value="{key}"', page
            )
        self.assertIn("data-ut-guard", page)
        response = self.client.post(
            self.step(self.row, "innehall"),
            {"sms_body": "Ny text från Exempelrör", "nasta": "mottagare"},
        )
        self.assertRedirects(response, self.step(self.row, "mottagare"))
        self.row.refresh_from_db()
        self.assertEqual(self.row.sms_body, "Ny text från Exempelrör")
        # Granska har inget formulär: chipsen är länkar där.
        review = self.client.get(self.step(self.row, "granska")).content.decode()
        self.assertNotIn('class="fl-ut-steps__link" name="nasta"', review)

    def test_change_contact_saves_the_text_first(self):
        url = self.step(self.row, "innehall")
        page = self.client.get(url)
        self.assertContains(page, f'name="byt_kontakt" value="{self.kontakter[1].pk}"')
        response = self.client.post(
            url, {"sms_body": "Hej {förnamn}, Exempelrör här.", "byt_kontakt": self.kontakter[1].pk}
        )
        self.assertRedirects(
            response, f"{url}?kontakt={self.kontakter[1].pk}", fetch_redirect_response=False
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.sms_body, "Hej {förnamn}, Exempelrör här.")
        foreign = make_contact(self.other_account, first_name="Hemlig", phone=phone(4999))
        self.assertEqual(
            self.client.post(url, {"sms_body": "x", "byt_kontakt": foreign.pk}).status_code, 400
        )

    def test_save_draft_leaves_the_audience_step_without_recipients(self):
        row = self.utskick(audience={})
        response = self.client.post(self.step(row, "mottagare"), {"namn": "X", "nasta": "lista"})
        self.assertRedirects(response, self.url("flamingo:app_utskick_list"))
        response = self.client.post(self.step(row, "mottagare"), {"namn": "X", "nasta": "tid"})
        self.assertRedirects(response, self.step(row, "tid"))
        response = self.client.post(self.step(row, "mottagare"), {"namn": "X", "nasta": "kanal"})
        self.assertRedirects(response, self.step(row, "mottagare"), fetch_redirect_response=False)

    def test_a_time_less_than_five_minutes_ahead_says_so(self):
        soon = timezone.localtime(timezone.now() + timedelta(minutes=2))
        response = self.client.post(
            self.step(self.row, "tid"),
            {"nar": "at", "datum": soon.date().isoformat(), "klockan": f"{soon:%H:%M}"},
        )
        self.assertContains(response, "Välj en tid minst 5 minuter fram.")

    def test_expensive_characters_are_named_and_the_box_is_always_there(self):
        url = self.step(self.row, "innehall")
        page = self.client.get(url).content.decode()
        self.assertIn('id="ut-gsm"', page)
        self.assertRegex(page, r'id="ut-gsm"[^>]*\shidden')
        self.assertIn('value="fix"', page)
        self.assertNotIn("Spara texten och tryck", page)
        text = "Hej" + chr(0xA0) + "Anna" + chr(0x200B) + ", Exempelrör" + chr(0x2019) + "s"
        data = self.client.post(
            self.url("flamingo:app_utskick_sms_preview", self.row), {"sms_body": text}
        ).json()
        self.assertEqual(
            data["non_gsm_text"],
            f"ett hårt mellanslag, ett osynligt tecken, {chr(0x2019)} (en typografisk apostrof)",
        )
        self.assertFalse(any("Byt automatiskt" in note["text"] for note in data["notes"]))
        self.client.post(url, {"sms_body": text})
        page = self.client.get(url).content.decode()
        self.assertNotRegex(page, r'id="ut-gsm"[^>]*\shidden')
        self.assertIn("Texten innehåller <span data-ut-gsm-chars>ett hårt mellanslag", page)

    def test_a_locked_page_option_says_how_to_unlock_it(self):
        Campaign.objects.filter(account=self.account).delete()
        page = self.client.get(self.step(self.row, "innehall"))
        self.assertContains(
            page,
            "Du har ingen publicerad Flamingo-sida än. Publicera en kampanj under Kampanjer, "
            "eller välj Egen adress.",
        )


class ReconfirmTests(UiFixture, TestCase):
    """UX-granskningen: ett pausat schemalagt utskick (sent, urvalet växte)
    gick bara att avbryta, eftersom Granska krävde en ny tid som inte gick
    att välja."""

    def setUp(self):
        super().setUp()
        self.person()
        self.row = self.utskick(
            status=Utskick.Status.PAUSED,
            pause_reason=Utskick.PauseReason.LATE,
            send_mode=Utskick.SendMode.AT,
            scheduled_at=timezone.now() - timedelta(hours=5),
            confirm_summary={"sms": 1},
        )
        self.state_url = self.url("flamingo:app_utskick_state", self.row)

    def test_a_late_utskick_is_confirmed_again_and_sent_now(self):
        response = self.client.get(self.step(self.row, "granska"))
        review = response.context["review"]
        self.assertTrue(review["send_now"])
        self.assertFalse(review["blocking"], review["items"])
        html = response.content.decode()
        self.assertNotIn("Tiden har passerat", html)
        self.assertIn("Det skickas när du bekräftar.", html)
        self.assertIn("Tillbaka till utskicket", html)
        self.assertNotIn('class="fl-ut-check__link"', html)
        self.assertEqual(html.count("fl-ut-steps__link is-locked"), 4)
        self.client.post(
            self.url("flamingo:app_utskick_confirm", self.row), {"nonce": response.context["nonce"]}
        )
        self.row.refresh_from_db()
        self.assertEqual(
            (self.row.status, self.row.send_mode), (Utskick.Status.SCHEDULED, Utskick.SendMode.NOW)
        )
        self.assertLess(abs((self.row.scheduled_at - timezone.now()).total_seconds()), 60)

    def test_a_later_time_is_kept_when_confirming_again(self):
        later = timezone.now() + timedelta(days=2)
        Utskick.objects.filter(pk=self.row.pk).update(
            pause_reason=Utskick.PauseReason.ACCOUNT_DISABLED, scheduled_at=later
        )
        review = self.client.get(self.step(self.row, "granska")).context["review"]
        self.assertFalse(review["send_now"])

    def test_the_utskick_can_be_reopened_and_cancel_asks_first(self):
        report = self.client.get(self.url("flamingo:app_utskick", self.row)).content.decode()
        self.assertIn("Ändra utskicket", report)
        self.assertIn("Inget mer skickas, och det går inte att ångra.", report)
        self.assertIn("<summary", report[report.index("fl-ut-banner") :])
        response = self.client.post(self.state_url, {"action": "andra"})
        self.assertRedirects(response, self.step(self.row, "tid"))
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.DRAFT)
        self.assertEqual(self.row.confirm_summary, {})
        # Fryst: går inte att öppna, bara att bekräfta igen eller avbryta.
        Utskick.objects.filter(pk=self.row.pk).update(
            status=Utskick.Status.PAUSED,
            pause_reason=Utskick.PauseReason.AUDIENCE_GREW,
            frozen_at=timezone.now(),
        )
        report = self.client.get(self.url("flamingo:app_utskick", self.row)).content.decode()
        self.assertNotIn("Ändra utskicket", report)
        self.assertIn("Avbryt utskicket", report)
        self.client.post(self.state_url, {"action": "andra"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.PAUSED)

    def test_a_content_pause_names_the_problem(self):
        AllowedHost.objects.create(
            account=self.account, host="grannen.example", status=AllowedHost.Status.REFUSED
        )
        TrackedLink.objects.filter(utskick=self.row).update(
            destination="https://grannen.example/boka"
        )
        Utskick.objects.filter(pk=self.row.pk).update(pause_reason=Utskick.PauseReason.CONTENT)
        report = self.client.get(self.url("flamingo:app_utskick", self.row))
        self.assertContains(report, "Utskicket stoppades innan det skickades.")
        self.assertContains(report, "ADX har inte godkänt länkar till grannen.example.")
        response = self.client.post(self.state_url, {"action": "andra"})
        self.assertRedirects(response, self.step(self.row, "innehall"))

    def test_change_on_a_scheduled_utskick_is_a_link_that_changes_nothing(self):
        Utskick.objects.filter(pk=self.row.pk).update(
            status=Utskick.Status.SCHEDULED,
            pause_reason="",
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        report = self.client.get(self.url("flamingo:app_utskick", self.row)).content.decode()
        self.assertIn(f'href="{self.step(self.row, "mottagare")}">Ändra</a>', report)
        self.assertNotIn('value="andra"', report)
        self.client.get(self.step(self.row, "mottagare"))
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, Utskick.Status.SCHEDULED)

    def test_the_demo_dialog_says_nothing_is_sent_once(self):
        Utskick.objects.filter(pk=self.row.pk).update(
            status=Utskick.Status.DRAFT, pause_reason="", send_mode=Utskick.SendMode.NOW
        )
        type(self.account).objects.filter(pk=self.account.pk).update(is_demo=True)
        html = self.client.get(self.step(self.row, "granska")).content.decode()
        self.assertEqual(html.count("Utskicket visas som skickat."), 1)
        self.assertNotIn("sms skickas nu", html)
        self.assertIn("Demokontot skickar aldrig. 1 sms visas som skickade.", html)
        self.assertIn("Visa som skickat", html)
