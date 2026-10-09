"""
Inkommande sms till det delade svarsnumret (README G.1, J S2 test_s2_inbound).

    WebhookTests        token, IP-listan (krävs i produktion), tom kropp, en
                        transaktion (fel ger 500 och inget sparas), idempotent id
    RoutingTests        en kandidat, flera (väntar på byrån), ingen, äldre,
                        svar utan avtal, slingskydd, "Ser ut som en avregistrering"
    PhraseTests         STOPP-tabellen (G.1)
    StopTests           STOPP hos flera kunder, bekräftelsen en gång per dygn
                        och taket före anropet, nödbromsen, demot, avstängt konto
    StartTests          START ger en bekräftelselänk och lyfter inget själv
    AgencyTests         byrån kopplar eller lägger åt sidan
    ReconcileTests      avstämningen mot 46elks historik
    OwnerNoticeTests    ägarens samlade sms, högst ett per 30 minuter
"""

from datetime import UTC, timedelta
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount, Lead, SmsLog
from apps.projects.models import Customer
from apps.sms.models import SmsAccount, SmsMessage
from apps.sms.tests import FakeElks

from . import codes, keys, threads
from .inbound import elks, routing, stop
from .models import (
    REKLAM,
    ConsentLog,
    Contact,
    DpaAcceptance,
    InboundMessage,
    LinkCode,
    Recipient,
    Suppression,
    Switchboard,
    Thread,
    ThreadMessage,
    Utskick,
    UtskickSettings,
)
from .testing import (
    PHONE_ANNA,
    PHONE_BO,
    PHONE_CILLA,
    UtskickFixture,
    enable_utskick,
    make_contact,
)

TOKEN = "t" * 32
#: send_out utan utskick=: Exempelrörs utskick; utskick=None: inget utskick.
DEFAULT = object()
REPLY_NUMBER = "+46766860046"

INBOUND_SETTINGS = {
    "UTSKICK_ELKS_INBOUND_TOKEN": TOKEN,
    "UTSKICK_REPLY_NUMBER": REPLY_NUMBER,
    "SMS_DLR_ALLOWED_IPS": ["127.0.0.1"],
    "SMS_SEND_LIVE": True,
    "ELKS_API_USERNAME": "test",
    "ELKS_API_PASSWORD": "test-losen",
    "ELKS_SENDER": "ADX",
    "SMS_PROVIDER": "46elks",
    "SMS_CALLBACK_BASE_URL": "",
    "SITE_BASE_URL": "https://adx.example",
    "INQUIRY_NOTIFICATION_EMAIL": "byra@adx.example",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}


class InboundFixture(UtskickFixture):
    """Två kunder med sms och utskick, Anna i Exempelrörs register och ett
    utskick som gått till henne från svarsnumret. Inställningarna
    (INBOUND_SETTINGS) gäller från setUp, så att en testmetod kan ändra dem."""

    def setUp(self):
        super().setUp()
        overridden = override_settings(**INBOUND_SETTINGS)
        overridden.enable()
        self.addCleanup(overridden.disable)
        Switchboard.get_solo()
        self.fake = FakeElks()
        patcher = mock.patch("apps.sms.elks._post", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        owner = mock.patch("apps.flamingo.sms._post_to_elks", return_value="o1")
        self.owner_post = owner.start()
        self.addCleanup(owner.stop)
        self.sms_account = SmsAccount.objects.create(
            customer=self.customer, is_enabled=True, sender_name="Exempelror"
        )
        self.other_sms = SmsAccount.objects.create(
            customer=self.other_customer, is_enabled=True, sender_name="Annanfirma"
        )
        self.kontakt = make_contact(
            self.account, first_name="Anna", last_name="Lind", phone=PHONE_ANNA
        )
        self.utskick = Utskick.objects.create(
            account=self.account,
            name="Höstservice värmepump",
            purpose=REKLAM,
            status=Utskick.Status.SENT,
        )
        self.client = Client(enforce_csrf_checks=True)
        self.n = 0

    # -- hjälpare ----------------------------------------------------------

    def send_out(self, sms_account=None, to=PHONE_ANNA, at=None, utskick=DEFAULT, kontakt=None):
        """Ett utskicks-sms från svarsnumret till numret (som slingan skriver)."""
        sms_account = sms_account or self.sms_account
        at = at or timezone.now() - timedelta(hours=1)
        message = SmsMessage.objects.create(
            account=sms_account,
            source="utskick",
            to=to,
            sender=REPLY_NUMBER,
            body="Hej Anna, dags för service hos Exempelrör. Svara STOPP för att inte få fler sms.",
            parts=1,
            status=SmsMessage.Status.DELIVERED,
            provider_id=f"o{SmsMessage.objects.count() + 1}",
            sent_at=at,
            created_at=at,
        )
        utskick = self.utskick if utskick is DEFAULT else utskick
        recipient = None
        if utskick is not None and sms_account == self.sms_account:
            recipient = Recipient.objects.create(
                utskick=utskick,
                contact=kontakt or self.kontakt,
                channel="sms",
                address=to,
                status=Recipient.Status.DELIVERED,
                sms_message=message,
                sent_at=at,
            )
        return message, recipient

    def fields(self, text, frm=PHONE_ANNA, **extra):
        self.n += 1
        data = {
            "id": f"in{self.n:030d}",
            "from": frm,
            "to": REPLY_NUMBER,
            "message": text,
            # 46elks skriver UTC utan tidszon.
            "created": timezone.now().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
            "direction": "incoming",
        }
        data.update(extra)
        return data

    def url(self, token=TOKEN):
        return reverse("utskick_api:elks_inbound", args=[token])

    def post(self, text, frm=PHONE_ANNA, token=TOKEN, data=None, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url(token), data or self.fields(text, frm, **extra))

    def handle(self, text, frm=PHONE_ANNA, now=None, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            message, _ = elks.handle(self.fields(text, frm, **extra), now=now)
        message.refresh_from_db()
        return message

    def thread_of(self, account=None):
        return Thread.objects.filter(account=account or self.account).select_related("lead").get()


class WebhookTests(InboundFixture, TestCase):
    def test_a_reply_is_stored_routed_and_answered_with_an_empty_body(self):
        self.send_out()
        response = self.post("Har ni tid tisdag förmiddag?")
        self.assertEqual((response.status_code, response.content), (200, b""))
        self.assertEqual(response.cookies, {})
        message = InboundMessage.objects.get()
        self.assertEqual(message.status, InboundMessage.Status.ROUTED)
        self.assertEqual(message.body, "")
        self.assertEqual(message.account, self.account)

    def test_wrong_token_ip_or_method_is_404_with_an_empty_body(self):
        self.send_out()
        calls = [
            self.post("Hej", token="x" * 32),
            self.client.get(self.url()),
            self.client.post(self.url(), self.fields("Hej"), REMOTE_ADDR="10.0.0.9"),
        ]
        for response in calls:
            self.assertEqual((response.status_code, response.content), (404, b""))
        self.assertFalse(InboundMessage.objects.exists())

    def test_a_token_outside_ascii_is_404_not_500(self):
        """Säkerhetsgranskningen S2: compare_digest kastade TypeError för
        text utanför ASCII, ett 500 utan inloggning (och ett fel i Sentry)."""
        response = self.client.post("/api/utskick/46elks/inkommande/%C3%A5%C3%A5%C3%A5/", {})
        self.assertEqual((response.status_code, response.content), (404, b""))
        self.assertFalse(elks._token_ok("ååå"))
        self.assertTrue(elks._token_ok(TOKEN))

    @override_settings(UTSKICK_ELKS_INBOUND_TOKEN="")
    def test_inbound_is_off_without_a_token(self):
        response = self.client.post(reverse("utskick_api:elks_inbound", args=["x"]), {"id": "a"})
        self.assertEqual((response.status_code, response.content), (404, b""))

    @override_settings(SMS_DLR_ALLOWED_IPS=[], DEBUG=False)
    def test_production_refuses_without_the_ip_list_and_alerts_once_a_day(self):
        self.send_out()
        for _ in range(2):
            response = self.post("Hej")
            self.assertEqual((response.status_code, response.content), (404, b""))
        self.assertFalse(InboundMessage.objects.exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("SMS_DLR_ALLOWED_IPS", mail.outbox[0].body)

    @override_settings(SMS_DLR_ALLOWED_IPS=[], DEBUG=True)
    def test_development_needs_only_the_token(self):
        self.send_out()
        self.assertEqual(self.post("Hej").status_code, 200)

    def test_a_missing_id_is_400(self):
        data = self.fields("Hej")
        del data["id"]
        response = self.client.post(self.url(), data)
        self.assertEqual((response.status_code, response.content), (400, b""))

    def test_the_same_46elks_id_twice_is_handled_once(self):
        self.send_out()
        data = self.fields("Har ni tid i morgon?")
        for _ in range(3):
            response = self.client.post(self.url(), data)
            self.assertEqual((response.status_code, response.content), (200, b""))
        self.assertEqual(InboundMessage.objects.count(), 1)
        self.assertEqual(ThreadMessage.objects.filter(direction="in").count(), 1)
        self.assertEqual(Lead.objects.filter(source=Lead.SOURCE_REPLY).count(), 1)

    def test_an_error_returns_500_and_stores_nothing(self):
        self.send_out()
        data = self.fields("Har ni tid?")
        with mock.patch.object(threads, "add_inbound", side_effect=RuntimeError("boom")):
            response = self.post("", data=data)
        self.assertEqual((response.status_code, response.content), (500, b""))
        self.assertFalse(InboundMessage.objects.exists())
        self.assertFalse(Thread.objects.exists())
        self.assertFalse(Lead.objects.filter(source=Lead.SOURCE_REPLY).exists())
        # 46elks försöker igen med samma id, och då går det.
        self.assertEqual(self.post("", data=data).status_code, 200)
        self.assertEqual(InboundMessage.objects.get().status, "routed")

    def test_json_bodies_work_too(self):
        self.send_out()
        response = self.client.post(self.url(), self.fields("Hej"), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(InboundMessage.objects.get().status, "routed")


class RoutingTests(InboundFixture, TestCase):
    def test_a_reply_to_an_utskick_becomes_a_thread_and_a_lead(self):
        message, recipient = self.send_out()
        inbound = self.handle("Har ni tid tisdag förmiddag?")
        self.assertEqual(inbound.routed_via, f"sms:{message.pk}")
        self.assertEqual(inbound.contact, self.kontakt)
        thread = self.thread_of()
        self.assertEqual(
            (thread.channel, thread.kind, thread.contact, thread.utskick, thread.address),
            ("sms", "reply", self.kontakt, self.utskick, PHONE_ANNA),
        )
        lead = thread.lead
        self.assertEqual((lead.source, lead.status), ("reply", "new"))
        self.assertEqual((lead.name, lead.phone), ("Anna Lind", "070-174 06 01"))
        self.assertEqual(lead.message, "Har ni tid tisdag förmiddag?")
        self.assertEqual(lead.contact, self.kontakt)
        self.assertIsNone(lead.utskick)
        self.assertFalse(lead.can_send_to_google)
        rows = list(thread.messages.order_by("at", "pk"))
        # Utskickets sms står först, som i mockupen, och svaret efter.
        self.assertEqual([r.direction for r in rows], ["out", "in"])
        self.assertEqual(rows[0].sms_message, message)
        self.assertEqual(rows[1].body, "Har ni tid tisdag förmiddag?")
        recipient.refresh_from_db()
        self.assertIsNotNone(recipient.replied_at)
        self.kontakt.refresh_from_db()
        self.assertEqual(self.kontakt.last_activity_kind, "reply")

    def test_a_second_reply_lands_in_the_same_thread_and_reopens_it(self):
        self.send_out()
        self.handle("Har ni tid?")
        thread = self.thread_of()
        Lead.objects.filter(pk=thread.lead_id).update(status=Lead.STATUS_CONTACTED)
        self.handle("Eller onsdag?")
        self.assertEqual(Thread.objects.count(), 1)
        thread.lead.refresh_from_db()
        self.assertEqual(thread.lead.status, Lead.STATUS_NEW)
        self.assertEqual(thread.lead.message, "Eller onsdag?")
        self.assertEqual(thread.messages.filter(direction="out").count(), 1)

    def test_a_reply_to_an_inbox_answer_goes_to_its_thread(self):
        self.send_out(at=timezone.now() - timedelta(days=40))
        self.handle("Hej", now=timezone.now() - timedelta(days=39))
        thread = self.thread_of()
        answer = SmsMessage.objects.create(
            account=self.sms_account,
            source="reply",
            to=PHONE_ANNA,
            sender=REPLY_NUMBER,
            body="Tisdag går bra /Exempelrör",
            status="sent",
            created_at=timezone.now() - timedelta(days=35),
        )
        ThreadMessage.objects.create(
            thread=thread, direction="out", body=answer.body, sms_message=answer, status="sent"
        )
        inbound = self.handle("Tack")
        self.assertEqual(inbound.routed_via, f"sms:{answer.pk}")
        self.assertEqual(Thread.objects.count(), 1)

    def test_several_customers_in_30_days_is_held_for_the_agency(self):
        self.send_out()
        self.send_out(self.other_sms, at=timezone.now() - timedelta(days=2))
        inbound = self.handle("Vem är det här?")
        self.assertEqual(inbound.status, InboundMessage.Status.AMBIGUOUS)
        self.assertEqual(inbound.body, "Vem är det här?")
        self.assertEqual(
            sorted(inbound.meta["candidates"]), sorted([self.account.pk, self.other_account.pk])
        )
        self.assertFalse(Thread.objects.exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn(PHONE_ANNA, mail.outbox[0].body)
        self.handle("Hallå")
        self.assertEqual(len(mail.outbox), 1, "högst ett larm i timmen")

    def test_nobody_sent_there_is_unroutable(self):
        inbound = self.handle("Hej", frm=PHONE_CILLA)
        self.assertEqual(inbound.status, InboundMessage.Status.UNROUTABLE)
        self.assertFalse(Thread.objects.exists())

    def test_an_older_send_still_routes_when_it_is_the_only_one(self):
        self.send_out(at=timezone.now() - timedelta(days=90))
        self.send_out(self.other_sms, at=timezone.now() - timedelta(days=120))
        inbound = self.handle("Hej igen")
        self.assertEqual((inbound.status, inbound.account), ("routed", self.account))

    def test_one_recent_customer_wins_over_older_ones(self):
        self.send_out(self.other_sms, at=timezone.now() - timedelta(days=60))
        self.send_out()
        self.assertEqual(self.handle("Hej").account, self.account)

    def test_rejected_or_failed_provider_sends_are_not_candidates(self):
        message, _ = self.send_out()
        SmsMessage.objects.filter(pk=message.pk).update(status="rejected")
        self.assertEqual(self.handle("Hej").status, InboundMessage.Status.UNROUTABLE)

    def test_without_a_current_dpa_the_thread_has_no_contact(self):
        from .models import DpaAcceptance

        DpaAcceptance.objects.filter(account=self.account).delete()
        self.send_out(to=PHONE_BO, utskick=None)
        inbound = self.handle("Hej", frm=PHONE_BO)
        thread = self.thread_of()
        self.assertIsNone(thread.contact)
        self.assertIsNone(inbound.contact)
        self.assertEqual(thread.lead.phone, "070-174 06 02")
        self.assertFalse(Contact.objects.filter(account=self.account, phone=PHONE_BO).exists())

    def test_with_a_dpa_an_unknown_number_becomes_a_contact(self):
        self.send_out(to=PHONE_BO, utskick=None)
        self.handle("Hej", frm=PHONE_BO)
        kontakt = Contact.objects.get(account=self.account, phone=PHONE_BO)
        self.assertEqual(kontakt.source, Contact.Source.REPLY)
        self.assertEqual(self.thread_of().contact, kontakt)

    def test_an_erased_number_never_comes_back_as_a_contact(self):
        Suppression.objects.create(
            account=self.account,
            channel="sms",
            value_hash=keys.value_hash("sms", PHONE_BO),
            reason=Suppression.Reason.ERASURE,
        )
        self.send_out(to=PHONE_BO, utskick=None)
        self.handle("Hej", frm=PHONE_BO)
        self.assertIsNone(self.thread_of().contact)

    def test_loop_guard_ignores_senders_that_are_not_mobile_numbers(self):
        self.send_out()
        for sender in ("Telia", "0701740601", "12345", REPLY_NUMBER):
            with self.subTest(sender=sender):
                inbound = self.handle("STOPP", frm=sender)
                self.assertEqual(inbound.status, InboundMessage.Status.IGNORED)
                self.assertEqual(inbound.body, "")
        self.assertFalse(Thread.objects.exists())
        self.assertFalse(Suppression.objects.exists())
        self.assertFalse(ThreadMessage.objects.filter(status="sending").exists())

    def test_only_the_reply_number_is_handled(self):
        self.send_out()
        inbound = self.handle("Hej", to="+46700000000")
        self.assertEqual((inbound.status, inbound.meta["reason"]), ("ignored", "to"))

    def test_a_flood_from_one_number_is_only_counted(self):
        self.send_out()
        for i in range(routing.FLOOD_PER_HOUR):
            self.handle(f"Hej {i}")
        inbound = self.handle("En till")
        self.assertEqual(inbound.status, InboundMessage.Status.COUNTED)
        self.assertEqual(inbound.body, "")
        self.assertEqual(
            ThreadMessage.objects.filter(direction="in").count(), routing.FLOOD_PER_HOUR
        )

    def test_unsubscribe_phrases_are_flagged_on_reklam_only(self):
        self.send_out()
        self.handle("Jag vill inte ha fler sms")
        self.assertTrue(self.thread_of().looks_like_stop)
        self.assertFalse(Suppression.objects.exists())
        Thread.objects.all().delete()
        Utskick.objects.filter(pk=self.utskick.pk).update(purpose="information")
        self.handle("Nej tack")
        self.assertFalse(self.thread_of().looks_like_stop)


class PhraseTests(TestCase):
    def test_the_stop_table(self):
        for text in (
            "STOPP",
            "stopp tack",
            "STOP!",
            "Stoppa",
            "Sluta skicka",
            "AVANMÄL",
            "unsubscribe",
            "STOPP jag har bytt nummer och vill inte ha fler",
            "  stopp.  ",
            "Avregistrera",
        ):
            with self.subTest(text=text):
                self.assertEqual(stop.classify(text), stop.STOP)
        for text in (
            "Stoppa inte min bokning",
            "Stopp inte",
            "Sluta inte skicka påminnelser",
            "Start",
            "Sluta skicka sms till mig nu för jag har bytt nummer",
            "Hej, kan ni stoppa leveransen",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(stop.classify(text), stop.STOP)

    def test_start(self):
        self.assertEqual(stop.classify("Start"), stop.START)
        self.assertEqual(stop.classify("START igen tack"), stop.START)
        self.assertEqual(stop.classify("Start av motorn går inte bra"), "")

    def test_looks_like_an_unsubscribe(self):
        for text in ("Avbryt", "Ta bort mig", "nej tack", "Jag vill inte ha fler sms"):
            with self.subTest(text=text):
                self.assertTrue(stop.looks_like_unsubscribe(text))
        for text in (
            "Stoppa inte min bokning",
            "Stopp inte",
            "Sluta inte skicka påminnelser",
            "Start",
            "Har ni tid tisdag?",
        ):
            with self.subTest(text=text):
                self.assertFalse(stop.looks_like_unsubscribe(text))

    def test_the_answer_texts(self):
        self.assertEqual(
            stop.stop_text(["Exempelrör"]),
            "Du får inga fler sms från Exempelrör. Svara START om du ångrar dig.",
        )
        self.assertEqual(
            stop.stop_text(["Exempelrör", "Annat AB"]),
            "Du får inga fler sms från Exempelrör och Annat AB. Svara START om du ångrar dig.",
        )
        self.assertEqual(
            stop.start_text("Exempelrör", "k.adx.se/b/Ab12Cd"),
            "Klicka för att få sms från Exempelrör igen: k.adx.se/b/Ab12Cd",
        )


class StopTests(InboundFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.other_utskick = Utskick.objects.create(
            account=self.other_account, name="Vinterkampanj", status=Utskick.Status.SENT
        )
        self.queued = Recipient.objects.create(
            utskick=Utskick.objects.create(
                account=self.account, name="Nästa", status=Utskick.Status.SENDING
            ),
            contact=self.kontakt,
            channel="sms",
            address=PHONE_ANNA,
        )

    def test_stopp_applies_to_every_recent_customer(self):
        _, recipient = self.send_out(at=timezone.now() - timedelta(days=3))
        self.send_out(self.other_sms, at=timezone.now() - timedelta(hours=2))
        inbound = self.handle("STOPP")
        self.assertEqual(inbound.status, InboundMessage.Status.STOP)
        self.assertEqual(inbound.body, "")
        self.assertEqual(inbound.meta["accounts"], [self.other_account.pk, self.account.pk])
        value_hash = keys.value_hash("sms", PHONE_ANNA)
        rows = Suppression.objects.filter(value_hash=value_hash, channel="sms")
        self.assertEqual({r.account_id for r in rows}, {self.account.pk, self.other_account.pk})
        self.assertTrue(all(r.reason == "stop" for r in rows))
        self.assertEqual(rows.get(account=self.account).utskick, self.utskick)
        consent = self.kontakt.consents.get(channel="sms")
        self.assertEqual(consent.status, "unsubscribed")
        log = ConsentLog.objects.filter(contact=self.kontakt, source="stop").get()
        self.assertEqual(log.by_label, "Svar STOPP")
        self.queued.refresh_from_db()
        self.assertEqual((self.queued.status, self.queued.skip_reason), ("skipped", "suppressed"))
        recipient.refresh_from_db()
        self.assertIsNotNone(recipient.stopped_at)
        # En STOPP-tråd hos varje kund, redan klar: badgen höjs inte.
        for account in (self.account, self.other_account):
            thread = self.thread_of(account)
            self.assertEqual(thread.kind, Thread.Kind.STOP)
            self.assertEqual(thread.lead.status, Lead.STATUS_CONTACTED)
            self.assertEqual(threads.status_label(thread.lead), "Avregistrerad automatiskt")
        self.assertFalse(Lead.objects.filter(status=Lead.STATUS_NEW).exists())
        # Ett svar i kö per kund, i den kundens tråd och med bara dess namn:
        # ingen kund ser vilka andra ADX-kunder som skickar till personen.
        answers = {
            a.thread.account_id: a
            for a in ThreadMessage.objects.filter(direction="out", inbound=inbound)
        }
        self.assertEqual(set(answers), {self.account.pk, self.other_account.pk})
        self.assertEqual(
            answers[self.other_account.pk].body,
            "Du får inga fler sms från Annanfirma. Svara START om du ångrar dig.",
        )
        self.assertEqual(
            answers[self.account.pk].body,
            "Du får inga fler sms från Exempelrör. Svara START om du ångrar dig.",
        )
        self.assertTrue(all(a.status == "sending" for a in answers.values()))
        self.assertEqual(self.fake.calls, [], "inget skickas i webbanropet")
        # Tickens sms: ett per kund, på kundens eget underlag.
        threads.send_due(timezone.now())
        sent = {m.account_id: m.body for m in SmsMessage.objects.filter(source="system")}
        self.assertEqual(
            sent,
            {
                self.sms_account.pk: answers[self.account.pk].body,
                self.other_sms.pk: answers[self.other_account.pk].body,
            },
        )

    def test_the_tick_sends_the_confirmation_from_the_reply_number(self):
        self.send_out()
        inbound = self.handle("Stopp tack")
        self.assertTrue(threads.work_exists(timezone.now()))
        counts = threads.send_due(timezone.now())
        self.assertEqual(counts["answers"], 1)
        sends = [c for c in self.fake.calls if c.get("dryrun") != "yes"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["from"], REPLY_NUMBER)
        self.assertEqual(sends[0]["to"], PHONE_ANNA)
        sms = SmsMessage.objects.get(reference=f"~x{inbound.pk}")
        self.assertEqual((sms.source, sms.account), ("system", self.sms_account))
        answer = ThreadMessage.objects.get(direction="out", inbound=inbound)
        self.assertEqual((answer.status, answer.sms_message), ("sent", sms))
        inbound.refresh_from_db()
        self.assertEqual(inbound.meta["answers"], {str(self.account.pk): "sent"})
        self.assertFalse(threads.answers_queued().exists())
        # Ingen andra gång.
        threads.send_due(timezone.now())
        self.assertEqual(SmsMessage.objects.filter(source="system").count(), 1)

    def test_a_report_that_came_before_the_link_still_reaches_the_answer(self):
        """Sändningsgranskningen: en leveransrapport mellan 46elks svar och
        kopplingen till trådens meddelande gick förlorad."""
        self.send_out()
        inbound = self.handle("STOPP")
        answer = ThreadMessage.objects.get(direction="out", inbound=inbound)
        sms = SmsMessage.objects.create(
            account=self.sms_account,
            source="system",
            to=PHONE_ANNA,
            sender=REPLY_NUMBER,
            body=answer.body,
            status=SmsMessage.Status.FAILED,
            parts=1,
        )
        ThreadMessage.objects.filter(pk=answer.pk).update(sms_message=sms, status="sent")
        threads._sync_late_report(sms)
        answer.refresh_from_db()
        self.assertEqual(answer.status, ThreadMessage.Status.FAILED)

    def test_at_most_one_confirmation_per_number_and_day(self):
        self.send_out()
        self.handle("STOPP")
        self.handle("STOPP!")
        self.assertEqual(
            ThreadMessage.objects.filter(direction="out", inbound__isnull=False).count(), 1
        )
        later = timezone.now() + timedelta(hours=25)
        self.handle("stopp", now=later)
        self.assertEqual(
            ThreadMessage.objects.filter(direction="out", inbound__isnull=False).count(), 2
        )

    def test_the_cap_is_checked_before_calling_apps_sms(self):
        SmsAccount.objects.filter(pk=self.sms_account.pk).update(monthly_cap_kr=0)
        self.send_out()
        inbound = self.handle("STOPP")
        threads.send_due(timezone.now())
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(SmsMessage.objects.filter(source="system").exists())
        answer = ThreadMessage.objects.get(direction="out", inbound=inbound)
        self.assertEqual(answer.status, "failed")
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())
        row = [r for r in threads.rows(answer.thread) if r.direction == "out"][-1]
        self.assertEqual(row.note, "Bekräftelsen skickades inte: kostnadstaket är nått.")

    def test_the_breaker_holds_the_confirmation(self):
        self.send_out()
        self.handle("STOPP")
        Switchboard.objects.update(sms_paused_until=timezone.now() + timedelta(minutes=10))
        threads.send_due(timezone.now())
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(threads.answers_queued().exists())
        Switchboard.objects.update(sms_paused_until=None)
        self.assertEqual(threads.send_due(timezone.now())["answers"], 1)

    def test_an_old_queued_answer_is_dropped(self):
        self.send_out()
        self.handle("STOPP")
        counts = threads.send_due(timezone.now() + threads.ANSWER_MAX_AGE + timedelta(minutes=1))
        self.assertEqual((counts["answers"], counts["skipped"]), (0, 1))
        self.assertEqual(self.fake.calls, [])

    def test_the_demo_never_sends_an_answer(self):
        self.send_out()
        inbound = self.handle("STOPP")
        self.account.is_demo = True
        self.account.save(update_fields=["is_demo"])
        threads.send_due(timezone.now())
        self.assertEqual(self.fake.calls, [])
        answer = ThreadMessage.objects.get(direction="out", inbound=inbound)
        self.assertEqual(answer.status, "failed")

    def test_stopp_works_for_a_disabled_account(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        self.send_out()
        self.handle("STOPP")
        self.assertTrue(Suppression.objects.filter(account=self.account, reason="stop").exists())

    def test_stopp_in_an_open_reply_thread_keeps_an_unread_question(self):
        self.send_out()
        self.handle("Vad kostar det?")
        self.handle("STOPP")
        thread = self.thread_of()
        self.assertEqual(thread.kind, Thread.Kind.STOP)
        self.assertEqual(thread.lead.status, Lead.STATUS_NEW)
        self.assertEqual(thread.messages.filter(direction="in").count(), 2)

    def test_stopp_without_a_contact_writes_the_consent_log_on_the_hash(self):
        self.send_out(to=PHONE_BO, utskick=None)
        self.handle("STOPP", frm=PHONE_BO)
        log = ConsentLog.objects.get(value_hash=keys.value_hash("sms", PHONE_BO))
        self.assertIsNone(log.contact)
        self.assertEqual((log.source, log.new_status), ("stop", "unsubscribed"))

    def test_stopp_from_an_unknown_number_waits_for_the_agency(self):
        inbound = self.handle("STOPP", frm=PHONE_CILLA)
        self.assertEqual(inbound.status, InboundMessage.Status.UNROUTABLE)
        self.assertEqual(inbound.meta["keyword"], "stop")
        self.assertFalse(Suppression.objects.exists())


class StartTests(InboundFixture, TestCase):
    def test_start_sends_a_confirm_link_and_lifts_nothing(self):
        self.send_out()
        self.handle("STOPP")
        threads.send_due(timezone.now())
        inbound = self.handle("Start")
        self.assertEqual(inbound.status, InboundMessage.Status.START)
        self.assertEqual(inbound.meta["answered"], [self.account.pk])
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())
        code = LinkCode.objects.get(kind="confirm")
        self.assertEqual(code.purpose, LinkCode.Purpose.START)
        self.assertEqual(code.value_hash, keys.value_hash("sms", PHONE_ANNA))
        self.assertEqual(code.contact, self.kontakt)
        self.assertEqual(codes.find(code.code, "confirm"), code)
        self.assertAlmostEqual(
            (code.expires_at - code.created_at).total_seconds(), 24 * 3600, delta=1
        )
        answer = ThreadMessage.objects.get(direction="out", inbound=inbound)
        self.assertTrue(answer.body.startswith("Klicka för att få sms från Exempelrör igen: "))
        self.assertTrue(answer.body.endswith(f"/b/{code.code}"))
        self.assertEqual(threads.send_due(timezone.now())["answers"], 1)
        self.assertEqual(
            SmsMessage.objects.get(reference=f"~x{inbound.pk}").source, SmsMessage.Source.SYSTEM
        )
        # START höjer inte badgen.
        self.assertFalse(Lead.objects.filter(status=Lead.STATUS_NEW).exists())

    def test_start_without_a_stopp_is_an_ordinary_reply(self):
        self.send_out()
        inbound = self.handle("Start")
        self.assertEqual(inbound.status, InboundMessage.Status.ROUTED)
        self.assertFalse(LinkCode.objects.exists())

    def test_start_twice_gives_one_link_a_day(self):
        self.send_out()
        self.handle("STOPP")
        self.handle("Start")
        self.handle("START")
        self.assertEqual(LinkCode.objects.filter(kind="confirm").count(), 1)


class AgencyTests(InboundFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.staff_client = Client()
        self.staff_client.force_login(self.staff)

    def route(self, inbound, **data):
        url = reverse("manage:utskick_inbound_route", args=[inbound.pk])
        return self.staff_client.post(url, data)

    def test_the_overview_lists_held_messages_masked(self):
        self.send_out()
        self.send_out(self.other_sms)
        self.handle("Vem är det här?")
        html = self.staff_client.get(reverse("manage:utskick_overview")).content.decode()
        self.assertIn('id="inkommande"', html)
        self.assertIn("Vem är det här?", html)
        self.assertIn("070-*** ** 01", html)
        self.assertNotIn(PHONE_ANNA, html)
        self.assertIn("Koppla till kund", html)

    def test_the_agency_routes_a_held_reply(self):
        self.send_out()
        self.send_out(self.other_sms)
        inbound = self.handle("Vem är det här?")
        response = self.route(inbound, action="route", account=self.other_account.pk)
        self.assertRedirects(
            response,
            reverse("manage:utskick_overview") + "#inkommande",
            fetch_redirect_response=False,
        )
        inbound.refresh_from_db()
        self.assertEqual((inbound.status, inbound.account), ("routed", self.other_account))
        self.assertEqual(inbound.routed_via, f"byrå:{self.staff.pk}")
        self.assertEqual(inbound.body, "")
        thread = self.thread_of(self.other_account)
        self.assertEqual(thread.messages.get(direction="in").body, "Vem är det här?")
        # Två gånger går inte.
        self.route(inbound, action="route", account=self.account.pk)
        self.assertFalse(Thread.objects.filter(account=self.account).exists())

    def test_a_held_message_goes_only_to_a_customer_that_texted_the_number(self):
        """Säkerhetsgranskningen S2: "Koppla till kund" fick välja vilken kund
        som helst med utskick, också demot. Nu bara kunder som skickat från
        svarsnumret till numret, och ingen kontakt skapas av en koppling."""
        inbound = self.handle("STOPP", frm=PHONE_CILLA)
        self.route(inbound, action="route", account=self.account.pk)
        inbound.refresh_from_db()
        self.assertEqual(inbound.status, InboundMessage.Status.UNROUTABLE)
        self.assertFalse(Suppression.objects.exists())
        self.assertFalse(Thread.objects.exists())
        html = self.staff_client.get(reverse("manage:utskick_overview")).content.decode()
        self.assertIn("Ingen kund har skickat sms till numret.", html)

        # Två kunder har skickat dit (den ena för länge sedan): båda kan väljas,
        # men inte demot eller en tredje kund.
        self.send_out(to=PHONE_BO, at=timezone.now() - timedelta(days=2))
        self.send_out(self.other_sms, to=PHONE_BO, at=timezone.now() - timedelta(days=90))
        demo_customer = Customer.objects.create(name="Demo AB")
        demo = FlamingoAccount.objects.create(customer=demo_customer, is_enabled=True, is_demo=True)
        enable_utskick(demo, "demo", "Demo")
        third = Customer.objects.create(name="Tredje AB")
        third_account = FlamingoAccount.objects.create(customer=third, is_enabled=True)
        enable_utskick(third_account, "tredje", "Tredje")
        held = InboundMessage.objects.create(
            channel="sms",
            provider_id="held-1",
            from_address=PHONE_BO,
            to_address=REPLY_NUMBER,
            body="Vem är det här?",
            received_at=timezone.now(),
            status=InboundMessage.Status.AMBIGUOUS,
            meta={"candidates": [self.account.pk]},
        )
        for refused in (demo, third_account):
            self.route(held, action="route", account=refused.pk)
            held.refresh_from_db()
            self.assertEqual(held.status, InboundMessage.Status.AMBIGUOUS, refused)
        self.assertFalse(Thread.objects.exists())
        self.route(held, action="route", account=self.other_account.pk)
        held.refresh_from_db()
        self.assertEqual((held.status, held.account), ("routed", self.other_account))
        self.assertEqual(self.thread_of(self.other_account).address, PHONE_BO)

    def test_routing_to_a_customer_without_an_sms_creates_no_contact(self):
        """Skyddet under vyn: route_held till en kund som aldrig skickat
        till numret ger en tråd men ingen kontakt."""
        third = Customer.objects.create(name="Tredje AB")
        third_account = FlamingoAccount.objects.create(customer=third, is_enabled=True)
        enable_utskick(third_account, "tredje", "Tredje")
        DpaAcceptance.objects.create(account=third_account, version=self.dpa, accepted_by=self.anna)
        inbound = self.handle("Hej", frm=PHONE_CILLA)
        routing.route_held(inbound.pk, third_account, user=self.staff, now=timezone.now())
        thread = self.thread_of(third_account)
        self.assertIsNone(thread.contact)
        self.assertFalse(third_account.utskick_contacts.exists())

    def test_ignore_and_bad_input(self):
        inbound = self.handle("Hej", frm=PHONE_CILLA)
        self.route(inbound, action="route", account="")
        self.route(inbound, action="route", account="999999")
        inbound.refresh_from_db()
        self.assertEqual(inbound.status, InboundMessage.Status.UNROUTABLE)
        self.route(inbound, action="ignore")
        inbound.refresh_from_db()
        self.assertEqual((inbound.status, inbound.body), ("ignored", ""))

    def test_customers_and_anonymous_cannot_route(self):
        inbound = self.handle("Hej", frm=PHONE_CILLA)
        url = reverse("manage:utskick_inbound_route", args=[inbound.pk])
        customer = Client()
        customer.force_login(self.anna)
        customer.post(url, {"action": "ignore"})
        Client().post(url, {"action": "ignore"})
        inbound.refresh_from_db()
        self.assertEqual(inbound.status, InboundMessage.Status.UNROUTABLE)


class ReconcileTests(InboundFixture, TestCase):
    def test_due_every_ten_minutes_when_inbound_is_on(self):
        now = timezone.now()
        self.assertTrue(elks.reconcile_due(now))
        with mock.patch.object(elks.elks_client, "list_messages", return_value=[]):
            elks.reconcile(now)
        self.assertFalse(elks.reconcile_due(now + timedelta(minutes=9)))
        self.assertTrue(elks.reconcile_due(now + timedelta(minutes=10)))
        with override_settings(UTSKICK_ELKS_INBOUND_TOKEN=""):
            self.assertFalse(elks.reconcile_due(now + timedelta(hours=1)))

    def test_a_missing_id_is_handled_and_a_known_one_is_skipped(self):
        self.send_out()
        known = self.handle("Hej")
        rows = [
            {
                "id": known.provider_id,
                "from": PHONE_ANNA,
                "to": REPLY_NUMBER,
                "message": "Hej",
                "created": timezone.now() - timedelta(minutes=30),
                "direction": "incoming",
            },
            {
                "id": "missad1",
                "from": PHONE_ANNA,
                "to": REPLY_NUMBER,
                "message": "STOPP",
                "created": timezone.now() - timedelta(minutes=20),
                "direction": "incoming",
            },
        ]
        with mock.patch.object(elks.elks_client, "list_messages", return_value=rows) as listed:
            counts = elks.reconcile(timezone.now())
        self.assertEqual(listed.call_args.kwargs["to"], REPLY_NUMBER)
        self.assertEqual(counts, {"checked": 2, "inserted": 1})
        missed = InboundMessage.objects.get(provider_id="missad1")
        self.assertEqual(missed.status, InboundMessage.Status.STOP)
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())

    def test_a_46elks_error_is_counted_not_raised(self):
        error = elks.elks_client.ElksError("nere")
        with mock.patch.object(elks.elks_client, "list_messages", side_effect=error):
            self.assertEqual(elks.reconcile(timezone.now())["failed"], 1)

    def test_it_reads_back_to_the_last_run_and_keeps_the_ticks_deadline(self):
        """Sändningsgranskningen: avstämningen läste alltid 48 timmar (upp till
        20 sidor à 10 s) och brydde sig inte om tickens tidsgräns."""
        now = timezone.now()
        with mock.patch.object(elks.elks_client, "list_messages", return_value=[]) as listed:
            elks.reconcile(now, deadline=123.0)
        self.assertEqual(listed.call_args.args[0], now - elks.RECONCILE_SINCE)
        self.assertEqual(listed.call_args.kwargs["deadline"], 123.0)
        later = now + timedelta(minutes=10)
        with mock.patch.object(elks.elks_client, "list_messages", return_value=[]) as listed:
            elks.reconcile(later)
        self.assertEqual(listed.call_args.args[0], now - elks.RECONCILE_MARGIN)
        # Ett fel: förra avstämningen står kvar, nästa tick läser samma period.
        error = elks.elks_client.ElksError("nere")
        with mock.patch.object(elks.elks_client, "list_messages", side_effect=error):
            elks.reconcile(later + timedelta(minutes=10))
        self.assertEqual(Switchboard.get_solo().last_elks_reconcile_at, later)
        self.assertTrue(elks.reconcile_due(later + timedelta(minutes=11)))
        partial = elks.elks_client.Messages()
        partial.complete = False
        with mock.patch.object(elks.elks_client, "list_messages", return_value=partial):
            self.assertEqual(elks.reconcile(later + timedelta(minutes=20))["partial"], 1)


class OwnerNoticeTests(InboundFixture, TestCase):
    def setUp(self):
        super().setUp()
        type(self.account).objects.filter(pk=self.account.pk).update(notify_phone="0701740699")
        self.account.refresh_from_db()

    def owner_sms(self):
        return list(SmsLog.objects.filter(kind=SmsLog.KIND_OWNER).order_by("pk"))

    def test_one_reply_gives_one_sms_with_the_reply(self):
        self.send_out()
        start = timezone.now()
        self.handle("Har ni tid tisdag?", now=start)
        counts = threads.send_due(start + timedelta(minutes=1))
        self.assertEqual(counts["notices"], 1)
        (row,) = self.owner_sms()
        lead = self.thread_of().lead
        self.assertEqual(
            row.body,
            "Svar på utskick från Anna Lind, 070-174 06 01: Har ni tid tisdag? Se mer: "
            f"https://adx.example/flamingo/app/inkorg/{lead.pk}/",
        )
        self.assertEqual(row.status, SmsLog.STATUS_SENT)
        self.assertEqual(row.lead, lead)

    def test_replies_are_batched_at_most_one_sms_per_30_minutes(self):
        self.send_out()
        start = timezone.now()
        self.handle("Ett", now=start)
        threads.send_due(start + timedelta(minutes=1))
        self.handle("Två", now=start + timedelta(minutes=5))
        self.handle("Tre", now=start + timedelta(minutes=6))
        self.assertFalse(threads.work_exists(start + timedelta(minutes=10)))
        self.assertEqual(threads.send_due(start + timedelta(minutes=10))["notices"], 0)
        later = start + timedelta(minutes=32)
        self.assertTrue(threads.work_exists(later))
        threads.send_due(later)
        rows = self.owner_sms()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[1].body,
            "2 nya svar på utskick. Se Inkorgen: https://adx.example/flamingo/app/inkorg/",
        )
        self.assertFalse(threads.work_exists(later + timedelta(hours=1)))

    def test_replies_and_utskick_leads_together(self):
        type(self.account).objects.filter(pk=self.account.pk).update(notify_sms=True)
        self.send_out()
        start = timezone.now()
        self.handle("Hej", now=start)
        Lead.objects.create(account=self.account, name="Bo", utskick=self.utskick, created_at=start)
        threads.send_due(start + timedelta(minutes=1))
        (row,) = self.owner_sms()
        self.assertEqual(
            row.body,
            "1 nytt svar och 1 förfrågan via utskick. Se Inkorgen: "
            "https://adx.example/flamingo/app/inkorg/",
        )

    def test_no_sms_when_the_customer_said_no(self):
        UtskickSettings.objects.filter(account=self.account).update(notify_on_reply=False)
        self.send_out()
        start = timezone.now()
        self.handle("Hej", now=start)
        self.assertFalse(threads.work_exists(start + timedelta(minutes=1)))
        Lead.objects.create(account=self.account, utskick=self.utskick, created_at=start)
        self.assertFalse(threads.work_exists(start + timedelta(minutes=1)), "notify_sms är av")

    def test_stopp_and_start_never_notify_the_owner(self):
        self.send_out()
        start = timezone.now()
        self.handle("STOPP", now=start)
        threads.send_due(start + timedelta(minutes=1))
        self.assertEqual(self.owner_sms(), [])

    def test_an_utskick_lead_gets_no_sms_of_its_own(self):
        from apps.flamingo import sms as flamingo_sms

        type(self.account).objects.filter(pk=self.account.pk).update(
            notify_sms=True, autoreply_enabled=False
        )
        self.account.refresh_from_db()
        lead = Lead.objects.create(account=self.account, name="Bo", utskick=self.utskick)
        rows = flamingo_sms.notify_new_lead(lead)
        self.assertEqual(rows[0].status, SmsLog.STATUS_DISABLED)
        self.assertEqual(rows[0].error, flamingo_sms.NOTE_VIA_UTSKICK)
        self.owner_post.assert_not_called()

    def test_owner_reply_texts(self):
        from apps.flamingo.sms import owner_reply_text

        url = "https://adx.example/flamingo/app/inkorg/"
        self.assertEqual(
            owner_reply_text(self.account, 3, 0, url), f"3 nya svar på utskick. Se Inkorgen: {url}"
        )
        self.assertEqual(
            owner_reply_text(self.account, 2, 1, url),
            f"2 nya svar och 1 förfrågan via utskick. Se Inkorgen: {url}",
        )
        self.assertEqual(
            owner_reply_text(self.account, 0, 2, url),
            f"2 nya förfrågningar via utskick. Se Inkorgen: {url}",
        )
        text = owner_reply_text(
            self.account,
            1,
            0,
            url + "9/",
            name="Johan <b>Berg</b> www.exempel.se",
            phone="073-555 12 34",
            text="Har ni tid? Se https://evil.example/x",
        )
        self.assertTrue(text.startswith("Svar på utskick från Johan b Berg b"), text)
        self.assertNotIn("evil", text)
        self.assertNotIn("<", text)
        self.assertTrue(text.endswith(f"Se mer: {url}9/"))


class SettingsTests(TestCase):
    def test_the_reply_number_setting_is_the_shared_number(self):
        self.assertEqual(settings.UTSKICK_REPLY_NUMBER, REPLY_NUMBER)
