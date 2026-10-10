"""Hela vägen genom S2 (README J S2, "Acceptance"), med de riktiga vyerna,
ticken och apps/sms. Varje del har sina egna tester; här prövas att
delarna hänger ihop:

    SendFlowTests       guiden, Granska, bekräftelsen, ticken (frysning och
                        sms-slingan), apps/sms med källan utskick,
                        leveransrapporten (hooks -> smsbridge) och rapporten
    ClickFlowTests      klicket på k.adx.se, landningssidan med ut,
                        besöksanropet, förfrågan med spåret och Inkorgen
    ReplyFlowTests      46elks inkommande, tråden i Inkorgen, svaret från
                        Inkorgen och nästa svar i samma tråd, ägarens sms
    StopFlowTests       STOPP, bekräftelsen via ticken, START och /b/
    DailyTests          utskick_daily räknar upp länkarna också

Inget når nätet: apps.sms.elks._post är FakeElks, avstämningens läsning av
46elks historik (elks.list_messages) svarar tomt och Flamingos ägarsms är
utbytt. Klockan är den riktiga (sms:ens created_at, minutgränsen, svarens
och ägarsms:ens fönster räknas på den), och utskickens tidsfönster hålls
öppet (timing.sms_window_open), så att testerna går också på natten;
fönstret prövas i test_s2_tick. Tickens budget går däremot på testklockan
(testing.TickClock), så att en lastad maskin inte får ticken att hoppa
över sms-fasen.
"""

import re
from datetime import UTC, timedelta
from unittest import mock
from urllib.parse import urlsplit

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import Lead
from apps.sms.models import SmsAccount, SmsMessage
from apps.sms.tests import FakeElks

from . import consent as consents
from . import reports
from .models import (
    CHANNEL_SMS,
    Click,
    Consent,
    ContactList,
    InboundMessage,
    LinkCode,
    Recipient,
    Suppression,
    Switchboard,
    Thread,
    ThreadMessage,
    Utskick,
)
from .sending import tick
from .test_s1_lp import LpFixture
from .test_s2_foundation import LINK_SETTINGS
from .testing import PHONE_ANNA, PHONE_BO, PHONE_CILLA, OnTickClock, make_contact

TOKEN = "f" * 32
REPLY = "+46766860046"
K = {"HTTP_HOST": "k.adx.se", "HTTP_ORIGIN": "null"}
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
CODE_RE = re.compile(r"k\.adx\.se/([A-Za-z0-9]{6})\b")
CONFIRM_RE = re.compile(r"k\.adx\.se/b/([A-Za-z0-9]{6})\b")
FN_RE = re.compile(r'name="fn" value="([^"]+)"')
BODY = "Hej {förnamn|du}, dags för service av badrummet hos Exempelrör. Boka:"

FLOW = {
    **LINK_SETTINGS,
    "SMS_SEND_LIVE": True,
    "ELKS_API_USERNAME": "test",
    "ELKS_API_PASSWORD": "test-losen",
    "ELKS_SENDER": "ADX",
    "SMS_PROVIDER": "46elks",
    "SMS_CALLBACK_BASE_URL": "https://adx.example",
    "SMS_DLR_ALLOWED_IPS": ["127.0.0.1"],
    "SMS_RATE_PER_MINUTE": 60,
    "SMS_GLOBAL_PER_MINUTE": 80,
    "UTSKICK_SMS_ACCOUNT_PER_MINUTE": 45,
    "UTSKICK_SMS_GLOBAL_PER_MINUTE": 60,
    "UTSKICK_REPLY_NUMBER": REPLY,
    "UTSKICK_ELKS_INBOUND_TOKEN": TOKEN,
    "SITE_BASE_URL": "https://adx.example",
    "INQUIRY_NOTIFICATION_EMAIL": "byran@adx.example",
}


class FlowFixture(OnTickClock, LpFixture):
    """Exempelrör med sms, svarsnumret och brytarna på, tre kontakter med
    samtycke i listan Kunder och en Flamingo-sida (Badrum Nacka)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sms_account = SmsAccount.objects.create(
            customer=cls.customer, is_enabled=True, sender_name="Exempelror"
        )
        cls.account.notify_phone = "070-174 06 99"
        cls.account.notify_sms = True
        cls.account.save(update_fields=["notify_phone", "notify_sms"])
        cls.kunder = ContactList.objects.create(account=cls.account, name="Kunder")
        cls.people = {}
        for first, phone in (("Anna", PHONE_ANNA), ("Bo", PHONE_BO), ("Cilla", PHONE_CILLA)):
            kontakt = make_contact(cls.account, first_name=first, last_name="Ek", phone=phone)
            consents.set_status(
                kontakt, CHANNEL_SMS, consents.YES, source="manual", evidence="kassan, 2025"
            )
            cls.kunder.memberships.create(contact=kontakt)
            cls.people[first] = kontakt
        now = timezone.now()
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK,
            defaults={"sms_enabled": True, "links_ready_at": now, "sms_inbound_ready_at": now},
        )

    def setUp(self):
        super().setUp()
        overridden = override_settings(**FLOW)
        overridden.enable()
        self.addCleanup(overridden.disable)
        self.fake = FakeElks()
        patcher = mock.patch("apps.sms.elks._post", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        owner = mock.patch("apps.flamingo.sms._post_to_elks", return_value="o1")
        self.owner_post = owner.start()
        self.addCleanup(owner.stop)
        window = mock.patch("apps.utskick.timing.sms_window_open", return_value=True)
        window.start()
        self.addCleanup(window.stop)
        # Ticken stämmer av mot 46elks historik (inkommande är på): ingen
        # historik här, så att ingen läsning försöker nå 46elks.
        history = mock.patch("apps.sms.elks.list_messages", return_value=[])
        self.history = history.start()
        self.addCleanup(history.stop)
        self.customer_client = self.client_for(self.anna)
        self.n = 0

    # -- kunden bygger och bekräftar ---------------------------------------

    def step(self, utskick, name):
        return reverse("flamingo:app_utskick_step", args=[utskick.pk, name])

    def build_and_confirm(self, when=None):
        """Guiden som kunden går den, Granska och bekräftelsen. when: en tid
        (Vid en tid), annars Skicka nu."""
        client = self.customer_client
        response = client.post(reverse("flamingo:app_utskick_new"))
        utskick = Utskick.objects.get(account=self.account)
        self.assertRedirects(response, self.step(utskick, "mottagare"))
        client.post(
            self.step(utskick, "mottagare"),
            {"namn": "Höstservice badrum", "lists": [self.kunder.pk], "nasta": "kanal"},
        )
        client.post(self.step(utskick, "kanal"), {"syfte": "reklam", "avsandare": "reply"})
        client.post(
            self.step(utskick, "innehall"),
            {
                "sms_body": BODY,
                "action": "lank",
                "lank_kampanj": self.quote_page.pk,
                "lank_nyckel": "boka",
                "lank_etikett": "Boka tid",
            },
        )
        utskick.refresh_from_db()
        self.assertEqual(utskick.sms_body, BODY + " {länk:boka}")
        response = client.post(
            self.step(utskick, "innehall"), {"sms_body": utskick.sms_body, "nasta": "tid"}
        )
        self.assertRedirects(response, self.step(utskick, "tid"))
        if when is None:
            data = {"nar": "now"}
        else:
            local = timezone.localtime(when)
            data = {"nar": "at", "datum": local.date().isoformat(), "klockan": f"{local:%H:%M}"}
        response = client.post(self.step(utskick, "tid"), data)
        self.assertRedirects(response, self.step(utskick, "granska"))
        page = client.get(self.step(utskick, "granska"))
        review = page.context["review"]
        self.assertFalse(review["blocking"], review["items"])
        response = client.post(
            reverse("flamingo:app_utskick_confirm", args=[utskick.pk]),
            {"nonce": page.context["nonce"]},
        )
        self.assertRedirects(response, reverse("flamingo:app_utskick", args=[utskick.pk]))
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SCHEDULED)
        self.assertEqual(utskick.confirmed_by, self.anna)
        self.assertFalse(utskick.confirmed_as_staff)
        self.assertEqual(utskick.confirm_summary["sms"], 3)
        return utskick

    def run_tick(self):
        with self.captureOnCommitCallbacks(execute=True):
            return tick.run(now=timezone.now(), budget=30)

    def sent_utskick(self):
        """Bekräftat med Skicka nu och skickat av ticken."""
        utskick = self.build_and_confirm()
        self.assertEqual(self.fake.sends, [])
        summary = self.run_tick()
        self.assertEqual(summary["status"], "worked", summary)
        self.assertEqual(summary.get("failed"), None, summary)
        utskick.refresh_from_db()
        return utskick

    def settle(self):
        """Ägarens sms tar inte med det som kom de sista sekunderna
        (threads.NOTICE_SETTLE): låt tiden gå för svaren och förfrågningarna."""
        earlier = timezone.now() - timedelta(seconds=30)
        InboundMessage.objects.update(created_at=earlier)
        Lead.objects.filter(account=self.account).update(created_at=earlier)

    def sms_to(self, phone):
        found = [c for c in self.fake.sends if c["to"] == phone]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def deliver(self, call):
        """46elks leveransrapport till adressen sms:et fick (whendelivered)."""
        message = SmsMessage.objects.get(provider_id=self.fake_id(call))
        path = urlsplit(call["whendelivered"]).path
        response = Client().post(
            path,
            {"id": message.provider_id, "status": "delivered", "delivered": "2026-10-13T10:01:00"},
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return message

    def fake_id(self, call):
        index = [c for c in self.fake.sends].index(call) + 1
        return f"s{index:032x}"

    # -- 46elks inkommande ------------------------------------------------

    def inbound(self, text, frm):
        self.n += 1
        data = {
            "id": f"flow{self.n:028d}",
            "from": frm,
            "to": REPLY,
            "message": text,
            "created": timezone.now().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
            "direction": "incoming",
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = Client(enforce_csrf_checks=True).post(
                reverse("utskick_api:elks_inbound", args=[TOKEN]), data
            )
        self.assertEqual((response.status_code, response.content), (200, b""))
        return InboundMessage.objects.get(provider_id=data["id"])


class SendFlowTests(FlowFixture, TestCase):
    def test_from_the_guide_to_the_report(self):
        utskick = self.sent_utskick()
        # Ticken frös, skickade och avslutade i samma körning.
        self.assertEqual(utskick.status, Utskick.Status.SENT)
        self.assertEqual(len(self.fake.sends), 3)
        self.assertEqual(utskick.frozen_counts["sms"], 3)
        for first, kontakt in self.people.items():
            call = self.sms_to(kontakt.phone)
            self.assertEqual(call["from"], REPLY)
            self.assertTrue(call["message"].startswith(f"Hej {first}, dags för service"))
            self.assertRegex(call["message"], CODE_RE)
            self.assertTrue(call["message"].endswith("Svara STOPP för att inte få fler sms."))
            self.assertTrue(call["whendelivered"].startswith("https://adx.example/api/sms/"))
        messages = SmsMessage.objects.filter(account=self.sms_account)
        self.assertEqual(set(messages.values_list("source", flat=True)), {"utskick"})
        recipients = Recipient.objects.filter(utskick=utskick)
        self.assertEqual(set(recipients.values_list("status", flat=True)), {"sent"})
        for recipient in recipients:
            self.assertEqual(recipient.sms_message.reference, f"~u{utskick.pk}:{recipient.pk}")
            self.assertEqual(recipient.sms_sender, REPLY)
            self.assertEqual(recipient.parts, 1)

        # Leveransrapporterna når mottagarna genom apps/sms hook.
        for call in self.fake.sends:
            self.deliver(call)
        self.assertEqual(set(recipients.values_list("status", flat=True)), {"delivered"})
        numbers = reports.summary(utskick)
        self.assertEqual((numbers["sent"], numbers["delivered"]), (3, 3))
        self.assertGreater(numbers["cost_units"], 0)
        page = self.customer_client.get(reverse("flamingo:app_utskick", args=[utskick.pk]))
        self.assertContains(page, "Höstservice badrum")
        delivered = self.customer_client.get(
            reverse("flamingo:app_utskick_recipients", args=[utskick.pk]), {"visa": "levererade"}
        )
        self.assertEqual(delivered.status_code, 200)
        self.assertContains(delivered, "Bo Ek")
        # Underlaget och portalen: utskicket står för sig.
        from apps.sms import pricing

        usage = pricing.usage(self.sms_account)
        self.assertEqual(usage["by_source"]["utskick"]["sms"], 3)
        # Ett andra varv gör ingenting mer.
        self.run_tick()
        self.assertEqual(len(self.fake.sends), 3)

    def test_nothing_goes_before_the_time(self):
        utskick = self.build_and_confirm(when=timezone.now() + timedelta(days=2))
        self.run_tick()
        self.assertEqual(self.fake.sends, [])
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SCHEDULED)
        self.assertFalse(Recipient.objects.filter(utskick=utskick).exists())

    def test_nothing_goes_with_the_switch_off_and_everything_after(self):
        utskick = self.build_and_confirm()
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(sms_enabled=False)
        self.run_tick()
        self.assertEqual(self.fake.sends, [])
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SENDING)
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(sms_enabled=True)
        self.run_tick()
        self.assertEqual(len(self.fake.sends), 3)
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SENT)


class ClickFlowTests(FlowFixture, TestCase):
    def test_click_landing_page_lead_and_inbox(self):
        utskick = self.sent_utskick()
        code = CODE_RE.search(self.sms_to(PHONE_ANNA)["message"]).group(1)
        k = Client(enforce_csrf_checks=True)
        response = k.get(f"/{code}", HTTP_USER_AGENT=IPHONE, **K)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.cookies, {})
        target = urlsplit(response["Location"])
        self.assertEqual(target.path, self.quote_page.landing_url)
        self.assertIn("ut=", target.query)
        self.assertIn(f"utm_campaign=utskick-{utskick.pk}", target.query)
        click = Click.objects.get(utskick=utskick)
        self.assertEqual(click.contact, self.people["Anna"])

        visitor = Client()
        page = visitor.get(f"{target.path}?{target.query}")
        self.assertEqual(page.status_code, 200)
        ut = dict(p.split("=", 1) for p in target.query.split("&"))["ut"]
        self.assertContains(page, f'name="ut" value="{ut}"')
        beacon = reverse("flamingo_public:visit_beacon", args=[self.quote_page.page_slug])
        self.assertEqual(visitor.post(beacon, {"ut": ut, "s": "42"}).status_code, 204)
        with self.captureOnCommitCallbacks(execute=True):
            response = visitor.post(
                self.quote_page.landing_url,
                {"name": "Anna Ek", "phone": "070-174 06 01", "q_storlek": "6", "ut": ut},
            )
        self.assertEqual(response.status_code, 302)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertEqual(lead.utskick, utskick)
        self.assertEqual(lead.contact, self.people["Anna"])
        self.assertEqual(lead.attribution["label"], "Boka tid")
        self.assertFalse(lead.can_send_to_google)
        click.refresh_from_db()
        self.assertEqual((click.lp_visits, click.engaged_seconds), (1, 42))

        numbers = reports.summary(utskick)
        self.assertEqual((numbers["clicked"], numbers["engaged"], numbers["leads"]), (1, 1, 1))
        inbox = self.customer_client.get(reverse("flamingo:app_inbox"))
        self.assertContains(inbox, "Utskick: Höstservice badrum")
        overview = self.customer_client.get(reverse("flamingo:app"))
        self.assertContains(overview, "Varav via utskick")
        # Ägaren får förfrågan i ett samlat sms från ticken, inte direkt.
        self.owner_post.assert_not_called()
        self.settle()
        self.run_tick()
        self.assertEqual(self.owner_post.call_count, 1)


class ReplyFlowTests(FlowFixture, TestCase):
    def test_a_reply_lands_in_the_inbox_and_is_answered_from_there(self):
        utskick = self.sent_utskick()
        inbound = self.inbound("Passar det tisdag förmiddag?", PHONE_BO)
        self.assertEqual(inbound.status, InboundMessage.Status.ROUTED)
        self.assertEqual(inbound.account, self.account)
        thread = Thread.objects.get(account=self.account)
        self.assertEqual((thread.utskick, thread.contact), (utskick, self.people["Bo"]))
        lead = thread.lead
        self.assertEqual((lead.source, lead.status), (Lead.SOURCE_REPLY, Lead.STATUS_NEW))
        self.assertIsNone(lead.utskick_id)
        recipient = Recipient.objects.get(utskick=utskick, contact=self.people["Bo"])
        self.assertIsNotNone(recipient.replied_at)
        self.assertEqual(reports.summary(utskick)["replied"], 1)
        # Utskickets sms står först i tråden, sedan svaret.
        self.assertEqual([m.direction for m in thread.messages.order_by("at", "pk")], ["out", "in"])

        client = self.customer_client
        inbox = client.get(reverse("flamingo:app_inbox"))
        self.assertContains(inbox, "Sms-svar")
        detail = client.get(reverse("flamingo:app_lead", args=[lead.pk]))
        self.assertContains(detail, "Passar det tisdag förmiddag?")
        self.assertContains(detail, "Utskick: Höstservice badrum")
        response = client.post(
            reverse("flamingo:app_lead_reply", args=[lead.pk]), {"text": "Tisdag 9 passar bra."}
        )
        self.assertEqual(response.status_code, 302)
        reply = self.fake.sends[-1]
        self.assertEqual((reply["to"], reply["from"]), (PHONE_BO, REPLY))
        self.assertEqual(reply["message"], "Tisdag 9 passar bra. /Exempelrör")
        out = ThreadMessage.objects.get(thread=thread, sent_by=self.anna)
        self.assertEqual(out.sms_message.source, "reply")
        self.deliver(reply)
        out.refresh_from_db()
        self.assertEqual(out.status, ThreadMessage.Status.SENT)

        # Nästa svar hamnar i samma tråd (svaret från Inkorgen var senast).
        lead.refresh_from_db()
        Lead.objects.filter(pk=lead.pk).update(status=Lead.STATUS_CONTACTED)
        self.inbound("Tack, vi ses.", PHONE_BO)
        self.assertEqual(Thread.objects.filter(account=self.account).count(), 1)
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.STATUS_NEW)
        self.assertEqual(lead.message, "Tack, vi ses.")

        # Ägaren får ett samlat sms om svaren från ticken.
        self.owner_post.assert_not_called()
        self.settle()
        self.run_tick()
        self.assertEqual(self.owner_post.call_count, 1)

    def test_staff_in_view_as_must_tick_the_box_and_is_recorded(self):
        self.sent_utskick()
        self.inbound("Har ni tid?", PHONE_BO)
        lead = Thread.objects.get(account=self.account).lead
        staff = self.client_for(self.staff)
        url = reverse("flamingo:app_lead_reply", args=[lead.pk])
        sends = len(self.fake.sends)
        response = staff.post(url, {"text": "Ja, ring oss."})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake.sends), sends)
        staff.post(url, {"text": "Ja, ring oss.", "staff_ok": "1"})
        self.assertEqual(len(self.fake.sends), sends + 1)
        out = ThreadMessage.objects.get(sent_by=self.staff)
        self.assertTrue(out.sent_as_staff)


class StopFlowTests(FlowFixture, TestCase):
    def test_stopp_confirmation_start_and_the_confirm_link(self):
        utskick = self.sent_utskick()
        inbound = self.inbound("STOPP", PHONE_CILLA)
        self.assertEqual(inbound.status, InboundMessage.Status.STOP)
        kontakt = self.people["Cilla"]
        suppression = Suppression.objects.get(account=self.account, channel=CHANNEL_SMS)
        self.assertEqual((suppression.reason, suppression.utskick), ("stop", utskick))
        recipient = Recipient.objects.get(utskick=utskick, contact=kontakt)
        self.assertIsNotNone(recipient.stopped_at)
        consent = Consent.objects.get(contact=kontakt, channel=CHANNEL_SMS)
        self.assertEqual(consent.status, consents.UNSUBSCRIBED)
        thread = Thread.objects.get(account=self.account)
        self.assertEqual(thread.kind, Thread.Kind.STOP)
        self.assertEqual(thread.lead.status, Lead.STATUS_CONTACTED)
        self.assertEqual(reports.summary(utskick)["stopped"], 1)
        inbox = self.customer_client.get(reverse("flamingo:app_inbox"))
        self.assertContains(inbox, "Avregistrerad automatiskt")

        # Bekräftelsen går med ticken, från svarsnumret, källan system.
        sends = len(self.fake.sends)
        self.run_tick()
        answer = self.fake.sends[sends]
        self.assertEqual((answer["to"], answer["from"]), (PHONE_CILLA, REPLY))
        self.assertEqual(
            answer["message"], "Du får inga fler sms från Exempelrör. Svara START om du ångrar dig."
        )
        self.assertEqual(
            SmsMessage.objects.get(reference=f"~x{inbound.pk}").source, SmsMessage.Source.SYSTEM
        )

        # START lyfter inget själv: länken i nästa sms gör det.
        start = self.inbound("Start", PHONE_CILLA)
        self.assertEqual(start.status, InboundMessage.Status.START)
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())
        self.run_tick()
        link = self.fake.sends[-1]
        code = CONFIRM_RE.search(link["message"]).group(1)
        self.assertEqual(LinkCode.objects.get(code=code).purpose, "start")
        k = Client(enforce_csrf_checks=True)
        page = k.get(f"/b/{code}", **K)
        self.assertContains(page, "Bekräfta att du vill få sms från Exempelrör igen.")
        self.assertEqual(page.cookies, {})
        fn = FN_RE.search(page.content.decode()).group(1)
        response = k.post(f"/b/{code}", {"fn": fn}, **K)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Suppression.objects.filter(account=self.account).exists())
        consent.refresh_from_db()
        self.assertEqual((consent.status, consent.source), (consents.YES, "start"))

    def test_a_stopp_before_the_send_skips_the_number(self):
        self.build_and_confirm()
        # Ett tidigare sms från svarsnumret gör kunden till kandidat.
        SmsMessage.objects.create(
            account=self.sms_account,
            source="utskick",
            to=PHONE_CILLA,
            sender=REPLY,
            body="Tidigare utskick",
            parts=1,
            status=SmsMessage.Status.DELIVERED,
            provider_id="tidigare1",
            created_at=timezone.now() - timedelta(days=2),
        )
        inbound = self.inbound("Stopp tack", PHONE_CILLA)
        self.assertEqual(inbound.status, InboundMessage.Status.STOP)
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())
        self.assertEqual(
            Consent.objects.get(contact=self.people["Cilla"], channel=CHANNEL_SMS).status,
            consents.UNSUBSCRIBED,
        )
        self.run_tick()
        utskick_sms = [c["to"] for c in self.fake.sends if c["message"].startswith("Hej ")]
        self.assertEqual(sorted(utskick_sms), [PHONE_ANNA, PHONE_BO])
        # Bekräftelsen av STOPP gick i samma tick.
        self.assertIn(
            (PHONE_CILLA, "Du får inga fler sms från Exempelrör. Svara START om du ångrar dig."),
            [(c["to"], c["message"]) for c in self.fake.sends],
        )
        utskick = Utskick.objects.get(account=self.account)
        skipped = Recipient.objects.get(utskick=utskick, contact=self.people["Cilla"])
        self.assertEqual((skipped.status, skipped.skip_reason), ("skipped", "suppressed"))


class DailyTests(FlowFixture, TestCase):
    def test_the_daily_run_rolls_up_the_links_too(self):
        """Ticken räknar bara upp länkarna när den har annat att göra;
        utskick_daily fångar klicken ändå (E.3)."""
        from . import retention
        from .models import TrackedLink

        utskick = self.sent_utskick()
        code = CODE_RE.search(self.sms_to(PHONE_BO)["message"]).group(1)
        Client().get(f"/{code}", HTTP_USER_AGENT=IPHONE, **K)
        link = TrackedLink.objects.get(utskick=utskick)
        self.assertEqual(link.human_clicks, 0)
        summary = retention.daily(timezone.now())
        self.assertEqual(summary["links"], {"links": 1})
        link.refresh_from_db()
        self.assertEqual(link.human_clicks, 1)
