"""
Sms från kontaktkortet och tidslinjens S4-delar (README I.4, I.7, H.1, H.6,
J S4; S4-HANDOFF.md rapport-byggaren).

    CardSmsTests        sms:et från svarsnumret med företagets namn, en ny tråd
                        som börjar som Klar, svaret i samma tråd, en öppen tråd
                        återanvänds, dubbeltryck, leverantörsfel
    CheckTests          varje kontroll före sändningen: spärren, fönstret, taket,
                        nödbromsen, Switchboard, stoppat konto, sms-kontot,
                        kollisionen på svarsnumret, demot, inget nummer
    StaffTests          byråns kryssruta och "Skicka som ADX", loggat med vem
    TenancyTests        en annan kunds kontakt är 404, utskick av är 404
    TimelineTests       sms:en på kortet, besök på egen sajt, bara kontots rader
    ReplyHabitTests     "Svarar oftast": färre än tre svar ger inget, kanal och
                        tid, lika många ger bara det som är säkert, STOPP räknas inte

Inget når nätet: apps.sms.elks._post är FakeElks (test_s2_inbound.InboundFixture).
Tidsfönstret står öppet i vyns tester (timing.sms_window_open), och prövas för
sig med en fast tid.
"""

from datetime import datetime, timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import Lead
from apps.sms.models import SmsMessage
from apps.sms.pricing import STOCKHOLM

from . import contact_sms, threads, timeline
from .access import Actor
from .models import (
    Event,
    InboundMessage,
    Suppression,
    Switchboard,
    Thread,
    ThreadMessage,
    UtskickSettings,
)
from .sending import checks
from .test_s2_inbound import REPLY_NUMBER, InboundFixture
from .testing import PHONE_ANNA, PHONE_BO, make_contact

TEXT = "Hej Anna, din reservdel har kommit. Vill du hämta den i morgon?"
BODY = f"{TEXT} /Exempelrör"


class CardFixture(InboundFixture):
    """Anna i Exempelrörs register, sms påslaget, fönstret öppet."""

    def setUp(self):
        super().setUp()
        Switchboard.objects.update(sms_enabled=True)
        self.app = self.client_for(self.anna)
        self.window = mock.patch("apps.utskick.timing.sms_window_open", return_value=True)
        self.window.start()
        self.window_open = True
        self.addCleanup(self.close_window)

    def close_window(self):
        if self.window_open:
            self.window.stop()
            self.window_open = False

    def url(self, kontakt=None):
        return reverse("flamingo:app_contact_sms", args=[(kontakt or self.kontakt).pk])

    def card(self, kontakt=None):
        return reverse("flamingo:app_contact", args=[(kontakt or self.kontakt).pk])

    def send(self, text=TEXT, client=None, kontakt=None, **extra):
        return (client or self.app).post(self.url(kontakt), {"text": text, **extra})

    def sends(self):
        return [c for c in self.fake.calls if c.get("dryrun") != "yes"]

    def actor(self, user=None, staff=False):
        return Actor(user=user or self.anna, label="Anna Lindqvist", staff=staff)


class CardSmsTests(CardFixture, TestCase):
    def test_the_card_has_the_button_and_the_form_opens(self):
        html = self.app.get(self.card()).content.decode()
        self.assertIn(self.url() + "#sms", html)
        self.assertIn(">Skicka sms</a>", html)
        self.assertNotIn('id="kt-sms-text"', html)
        response = self.app.get(self.url())
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="kt-sms-text"', html)
        self.assertIn('data-ut-sms-suffix=" /Exempelrör"', html)
        self.assertIn("Skickas från 0766 86 00 46", html)
        self.assertIn("js/flamingo-app-utskick.js", html)
        self.assertNotIn("som_adx", html)

    def test_a_contact_without_a_number_has_no_button(self):
        kontakt = make_contact(self.account, first_name="Bo", email="bo@exempel.example")
        self.assertNotIn(self.url(kontakt), self.app.get(self.card(kontakt)).content.decode())
        self.assertContains(self.app.get(self.url(kontakt)), checks.NO_PHONE_TEXT)
        response = self.send(kontakt=kontakt)
        self.assertContains(response, checks.NO_PHONE_TEXT)
        self.assertEqual(self.sends(), [])

    def test_the_sms_goes_from_the_reply_number_with_the_name_last(self):
        response = self.send()
        self.assertRedirects(response, self.card() + "#tidslinje", fetch_redirect_response=False)
        (send,) = self.sends()
        self.assertEqual(
            (send["from"], send["to"], send["message"]), (REPLY_NUMBER, PHONE_ANNA, BODY)
        )
        message = ThreadMessage.objects.get(direction="out", sent_by=self.anna)
        self.assertEqual(
            (message.body, message.status, message.sent_as_staff), (BODY, "sent", False)
        )
        sms = message.sms_message
        self.assertEqual((sms.source, sms.reference), ("reply", f"~t{message.pk}"))
        html = self.app.get(self.card()).content.decode()
        self.assertIn(contact_sms.SENT_TEXT, html)
        self.assertIn(timeline.CONTACT_SMS_TITLE, html)
        self.assertIn("Skickat av Anna", html)

    def test_the_name_is_not_added_twice(self):
        self.send("Hej, Exempelrör här. Din reservdel har kommit.")
        self.assertEqual(
            self.sends()[0]["message"], "Hej, Exempelrör här. Din reservdel har kommit."
        )

    def test_a_new_thread_is_direct_and_never_raises_the_badge(self):
        self.send()
        thread = Thread.objects.get(account=self.account)
        self.assertEqual(
            (thread.kind, thread.channel, thread.address, thread.contact),
            (Thread.Kind.DIRECT, "sms", PHONE_ANNA, self.kontakt),
        )
        self.assertFalse(thread.unread)
        self.assertEqual(thread.lead.status, Lead.STATUS_CONTACTED)
        self.assertEqual(thread.lead.source, Lead.SOURCE_REPLY)
        self.assertEqual(thread.lead.message, BODY)
        response = self.app.get(reverse("flamingo:app_inbox"))
        self.assertEqual(response.context["new_lead_count"], 0)

    def test_the_card_sms_is_no_inquiry_on_the_card(self):
        self.send()
        thread = Thread.objects.get(account=self.account)
        self.assertIsNone(thread.lead.contact)
        kinds = [item.kind for item in timeline.for_contact(self.kontakt).items]
        self.assertNotIn("lead", kinds)
        self.assertNotContains(self.app.get(self.card()), "1 förfrågan")
        detail = self.app.get(reverse("flamingo:app_lead", args=[thread.lead_id]))
        self.assertContains(detail, self.card())

    def test_the_reply_lands_in_the_same_thread_and_raises_it(self):
        self.send()
        thread = Thread.objects.get(account=self.account)
        sms = ThreadMessage.objects.get(direction="out", thread=thread).sms_message
        inbound = self.handle("Ja tack, i morgon passar.")
        self.assertEqual(inbound.status, InboundMessage.Status.ROUTED)
        self.assertEqual(inbound.routed_via, f"sms:{sms.pk}")
        self.assertEqual(Thread.objects.filter(account=self.account).count(), 1)
        reply = ThreadMessage.objects.get(direction="in")
        self.assertEqual(reply.thread, thread)
        thread.lead.refresh_from_db()
        self.assertEqual(thread.lead.status, Lead.STATUS_NEW)

    def test_an_open_reply_thread_is_used_and_marked_done(self):
        self.send_out()
        self.handle("Har ni tid tisdag?")
        thread = Thread.objects.get(account=self.account)
        self.assertEqual(thread.lead.status, Lead.STATUS_NEW)
        self.send()
        self.assertEqual(Thread.objects.filter(account=self.account).count(), 1)
        self.assertTrue(
            ThreadMessage.objects.filter(thread=thread, direction="out", sent_by=self.anna).exists()
        )
        thread.lead.refresh_from_db()
        self.assertEqual(thread.lead.status, Lead.STATUS_CONTACTED)

    def test_a_double_click_sends_once(self):
        self.send()
        response = self.send()
        self.assertEqual(len(self.sends()), 1)
        self.assertRedirects(response, self.card() + "#tidslinje", fetch_redirect_response=False)
        self.assertContains(self.app.get(self.card()), contact_sms.DUPLICATE_TEXT)

    def test_empty_and_too_long_keep_the_text(self):
        self.assertContains(self.send("   "), contact_sms.EMPTY_TEXT)
        text = "a" * 950
        response = self.send(text)
        self.assertContains(response, contact_sms.TOO_LONG_TEXT)
        self.assertContains(response, text + "</textarea>")
        self.assertEqual(self.sends(), [])
        self.assertFalse(Thread.objects.exists())

    def test_a_provider_failure_keeps_the_text_and_leaves_no_thread(self):
        leads = Lead.objects.count()
        self.fake.fail_send = "46elks svarade 403"
        response = self.send()
        self.assertContains(response, contact_sms.SEND_ERRORS["provider_error"])
        self.assertContains(response, TEXT + "</textarea>")
        self.assertFalse(Thread.objects.exists())
        self.assertFalse(ThreadMessage.objects.filter(direction="out").exists())
        self.assertEqual(Lead.objects.count(), leads)

    def test_get_never_sends(self):
        self.app.get(self.url())
        self.app.head(self.url())
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.app.put(self.url()).status_code, 405)


class CheckTests(CardFixture, TestCase):
    def assertRefused(self, text, kontakt=None):
        response = self.send(kontakt=kontakt)
        self.assertContains(response, text)
        self.assertEqual(self.sends(), [])
        self.assertFalse(ThreadMessage.objects.filter(direction="out").exists())
        self.assertContains(self.app.get(self.url(kontakt)), text)

    def test_a_stopped_number_is_refused(self):
        threads.suppress_number(
            self.account,
            PHONE_ANNA,
            reason=Suppression.Reason.STOP,
            source="reply",
            actor=self.actor(),
        )
        self.assertRefused(checks.CONTACT_SUPPRESSED_TEXT)

    def test_the_window_is_checked_at_the_given_time(self):
        self.close_window()
        late = datetime(2026, 10, 7, 23, 0, tzinfo=STOCKHOLM)
        self.assertIn("tidsfönster", contact_sms.problem(self.kontakt, now=late))
        result = contact_sms.send(self.kontakt, TEXT, actor=self.actor(), now=late)
        self.assertFalse(result.ok)
        self.assertIn("Du kan skicka 09.00", result.error)
        self.assertEqual(self.sends(), [])
        noon = datetime(2026, 10, 7, 12, 0, tzinfo=STOCKHOLM)
        self.assertEqual(contact_sms.problem(self.kontakt, now=noon), "")

    def test_the_cost_cap_is_refused_and_says_how_to_lift_it(self):
        with mock.patch(
            "apps.sms.pricing.usage", return_value={"cap_reached": True, "remaining": 0}
        ):
            self.sms_account.customer_manages_api = False
            self.sms_account.save(update_fields=["customer_manages_api"])
            self.assertRefused(checks.CAP_ADX_TEXT)
            self.sms_account.customer_manages_api = True
            self.sms_account.save(update_fields=["customer_manages_api"])
            self.assertContains(self.app.get(self.url()), checks.CAP_OWN_TEXT)

    def test_a_cap_too_small_for_this_sms_is_refused(self):
        with mock.patch("apps.utskick.threads.cap_allows", return_value=False):
            response = self.send()
        self.assertContains(response, contact_sms.SEND_ERRORS["monthly_cap_reached"])
        self.assertEqual(self.sends(), [])

    def test_the_breaker_is_refused(self):
        Switchboard.objects.update(sms_paused_until=timezone.now() + timedelta(minutes=5))
        self.assertRefused(checks.BREAKER_TEXT)

    def test_sms_off_in_the_switchboard_is_refused(self):
        Switchboard.objects.update(sms_enabled=False)
        self.assertRefused(checks.SMS_OFF_TEXT)

    def test_a_blocked_account_is_refused(self):
        UtskickSettings.objects.filter(account=self.account).update(sending_blocked=True)
        self.assertRefused(checks.BLOCKED_TEXT)

    def test_a_disabled_sms_account_is_refused(self):
        self.sms_account.is_enabled = False
        self.sms_account.save(update_fields=["is_enabled"])
        self.assertRefused(checks.SMS_ACCOUNT_TEXT)

    def test_a_recent_reply_number_sms_from_another_customer_is_refused(self):
        self.send_out(sms_account=self.other_sms, at=timezone.now() - timedelta(days=20))
        # Granskningen: en GET på rutan fick inte avslöja att en annan kund
        # skickat till numret. Kollisionen prövas bara när sms:et skickas,
        # och texten nämner aldrig den andra kunden.
        page = self.app.get(self.url())
        self.assertContains(page, 'name="text"')
        self.assertNotContains(page, checks.COLLISION_TEXT)
        self.assertEqual(contact_sms.problem(self.kontakt), "")
        with self.assertLogs("apps.utskick.sending.checks", "INFO") as logged:
            response = self.send()
        self.assertContains(response, checks.COLLISION_TEXT)
        self.assertNotContains(response, "ADX-kund")
        self.assertIn(f"kontakt {self.kontakt.pk}", logged.output[0])
        self.assertIn("en annan kund", logged.output[0])
        self.assertEqual(self.sends(), [])
        self.assertFalse(ThreadMessage.objects.filter(direction="out").exists())

    def test_an_old_reply_number_sms_from_another_customer_is_fine(self):
        self.send_out(sms_account=self.other_sms, at=timezone.now() - timedelta(days=40))
        SmsMessage.objects.filter(account=self.other_sms).update(
            created_at=timezone.now() - timedelta(days=40)
        )
        self.send()
        self.assertEqual(len(self.sends()), 1)

    def test_the_demo_never_sends(self):
        self.account.is_demo = True
        self.account.save(update_fields=["is_demo"])
        self.assertRefused("Demokontot skickar aldrig.")
        # Granskningen: demot fick bara meningen; nu visar rutan formuläret
        # med raden om demot, som Inkorgens svar.
        page = self.app.get(self.url())
        self.assertContains(page, 'name="text"')
        self.assertContains(page, '<p class="fl-field__help">Demokontot skickar aldrig.</p>')
        self.assertEqual(contact_sms.problem(self.kontakt), "")
        result = contact_sms.send(self.kontakt, TEXT, actor=self.actor())
        self.assertEqual((result.ok, result.error), (False, "Demokontot skickar aldrig."))
        self.assertEqual(self.sends(), [])

    def test_a_deleted_contact_is_not_reached(self):
        kontakt = make_contact(self.account, first_name="Bo", phone=PHONE_BO)
        kontakt.delete()
        result = contact_sms.send(kontakt, TEXT, actor=self.actor())
        self.assertFalse(result.ok)
        self.assertEqual(self.sends(), [])


class StaffTests(CardFixture, TestCase):
    def test_staff_must_tick_the_box_and_is_logged(self):
        staff = self.client_for(self.staff)
        html = staff.get(self.url()).content.decode()
        self.assertIn('name="som_adx"', html)
        self.assertIn("Jag skickar det här som ADX åt Exempelrör.", html)
        self.assertIn(">Skicka som ADX</button>", html)
        response = self.send(client=staff)
        self.assertContains(response, "Kryssa i att du skickar som ADX åt kunden.")
        self.assertEqual(self.sends(), [])
        with self.assertLogs("apps.utskick.contact_sms", "INFO") as logs:
            self.send(client=staff, som_adx="1")
        message = ThreadMessage.objects.get(direction="out", sent_by=self.staff)
        self.assertTrue(message.sent_as_staff)
        self.assertIn(f"användare {self.staff.pk} (ADX åt kunden)", logs.output[0])
        self.assertNotIn(PHONE_ANNA, logs.output[0])
        html = self.app.get(self.card()).content.decode()
        self.assertIn("Skickat av ADX (", html)
        self.assertIn("åt kunden", html)


class TenancyTests(CardFixture, TestCase):
    def test_another_accounts_contact_is_404(self):
        foreign = make_contact(self.other_account, first_name="Hemlig", phone=PHONE_BO)
        self.assertEqual(self.app.get(self.url(foreign)).status_code, 404)
        self.assertEqual(self.send(kontakt=foreign).status_code, 404)
        self.assertEqual(self.sends(), [])

    def test_utskick_off_is_404_also_for_staff(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        for client in (self.app, self.client_for(self.staff)):
            self.assertEqual(client.get(self.url()).status_code, 404)
            self.assertEqual(self.send(client=client, som_adx="1").status_code, 404)
        self.assertEqual(self.sends(), [])


class TimelineTests(CardFixture, TestCase):
    def items(self, kontakt=None):
        return timeline.for_contact(kontakt or self.kontakt).items

    def test_card_sms_and_inbox_replies_are_on_the_card_but_not_utskick_sms(self):
        self.send_out()
        self.send()
        kinds = [item.kind for item in self.items()]
        self.assertEqual(kinds.count("sms_out"), 1)
        (item,) = [i for i in self.items() if i.kind == "sms_out"]
        self.assertIn(BODY[:60], item.detail)
        self.assertEqual(item.link_label, "Öppna i Inkorgen")

    def test_only_the_accounts_rows(self):
        """Ett annat kontos tråd som (manipulerat) pekar på Annas kontakt
        syns inte på Annas kort."""
        lead = Lead.objects.create(account=self.other_account, source=Lead.SOURCE_REPLY)
        foreign = Thread.objects.create(
            account=self.other_account,
            contact=self.kontakt,
            channel="sms",
            kind=Thread.Kind.DIRECT,
            lead=lead,
            address=PHONE_ANNA,
        )
        ThreadMessage.objects.create(
            thread=foreign, direction="out", body="Hemligt", sent_by=self.anna
        )
        for n in range(3):
            ThreadMessage.objects.create(thread=foreign, direction="in", body=f"Hemligt {n}")
        Event.objects.create(
            account=self.other_account,
            contact=self.kontakt,
            kind=Event.SITE_VISIT,
            data={"sida": "/hemligt", "varde": "annanfirma.example"},
        )
        details = " ".join(f"{i.title} {i.detail}" for i in self.items())
        self.assertNotIn("Hemligt", details)
        self.assertNotIn("annanfirma", details)
        self.assertEqual(timeline.reply_habit(self.kontakt), "")

    def test_a_site_visit_shows_the_page(self):
        Event.objects.create(
            account=self.account,
            contact=self.kontakt,
            utskick=self.utskick,
            kind=Event.SITE_VISIT,
            data={"klick": 1, "sida": "/priser", "varde": "exempelror.example"},
        )
        (item,) = [i for i in self.items() if i.kind == Event.SITE_VISIT]
        self.assertEqual(item.title, "Besökte webbplatsen")
        self.assertEqual(item.detail, "Höstservice värmepump · exempelror.example/priser")
        html = self.app.get(self.card()).content.decode()
        self.assertIn("Besökte webbplatsen", html)

    def test_a_goal_on_the_site_has_its_name(self):
        Event.objects.create(
            account=self.account,
            contact=self.kontakt,
            kind=Event.SITE_VISIT,
            data={"klick": 1, "sida": "/boka", "varde": "exempelror.example", "mal": "bokning"},
        )
        (item,) = [i for i in self.items() if i.kind == Event.SITE_VISIT]
        self.assertEqual(item.title, "Gjorde på webbplatsen: bokning")
        self.assertEqual(item.detail, "exempelror.example/boka")

    def test_a_site_visit_detail_never_shows_a_query(self):
        event = Event(
            account=self.account,
            contact=self.kontakt,
            kind=Event.SITE_VISIT,
            data={"sida": "/boka?adx=hemligt#topp", "varde": "exempelror.example"},
        )
        self.assertEqual(timeline.site_visit_detail(event), "exempelror.example/boka")


class ReplyHabitTests(CardFixture, TestCase):
    BODIES = {"stop": "STOPP", "start": "START"}

    def reply(self, hour, channel="sms", keyword="", days=0, routed=True):
        """Ett svar som routing.route_to sparar det: en InboundMessage vars
        meta saknar "keyword" för ett vanligt svar (routed=False: raden är
        gallrad och ThreadMessage.inbound är NULL)."""
        thread = Thread.objects.filter(account=self.account, channel=channel).first()
        if thread is None:
            lead = Lead.objects.create(account=self.account, source=Lead.SOURCE_REPLY)
            thread = Thread.objects.create(
                account=self.account,
                contact=self.kontakt,
                channel=channel,
                lead=lead,
                address=PHONE_ANNA if channel == "sms" else "anna@exempel.example",
            )
        inbound = None
        if routed:
            self.n += 1
            inbound = InboundMessage.objects.create(
                channel=channel,
                provider_id=f"vana{self.n}",
                received_at=timezone.now(),
                meta={"keyword": keyword} if keyword else {},
            )
        day = timezone.localtime(timezone.now(), STOCKHOLM).date() - timedelta(days=days + 1)
        at = datetime(day.year, day.month, day.day, hour, 15, tzinfo=STOCKHOLM)
        body = self.BODIES.get(keyword, "Svar")
        return ThreadMessage.objects.create(
            thread=thread, direction="in", body=body, at=at, inbound=inbound
        )

    def test_routed_replies_count(self):
        # Granskningen: ett exclude på inbound__meta__keyword i SQL tappade
        # varje svar vars meta saknar nyckeln (NOT(NULL)).
        for days in range(3):
            self.reply(19, days=days)
        self.assertEqual(
            InboundMessage.objects.filter(meta={}).count(), 3, "svaren har en tom meta"
        )
        self.assertEqual(timeline.reply_habit(self.kontakt), "Svarar oftast på sms, kvällstid")

    def test_stop_and_start_after_the_inbound_rows_are_purged(self):
        self.reply(19)
        self.reply(20, days=1)
        self.reply(21, keyword="stop", days=2, routed=False)
        self.reply(19, keyword="start", days=3, routed=False)
        self.assertEqual(timeline.reply_habit(self.kontakt), "")
        self.reply(18, days=4, routed=False)
        self.assertEqual(timeline.reply_habit(self.kontakt), "Svarar oftast på sms, kvällstid")

    def test_fewer_than_three_replies_give_nothing(self):
        self.reply(19)
        self.reply(20, days=1)
        self.assertEqual(timeline.reply_habit(self.kontakt), "")
        self.assertNotIn("Svarar oftast", self.app.get(self.card()).content.decode())

    def test_three_evening_sms_replies(self):
        for days in range(3):
            self.reply(18 + days, days=days)
        self.assertEqual(timeline.reply_habit(self.kontakt), "Svarar oftast på sms, kvällstid")
        self.assertContains(self.app.get(self.card()), "Svarar oftast på sms, kvällstid")

    def test_a_tie_leaves_that_part_out(self):
        self.reply(8)
        self.reply(12, days=1)
        self.reply(19, days=2)
        self.assertEqual(timeline.reply_habit(self.kontakt), "Svarar oftast på sms")
        self.reply(13, channel="email", days=3)
        self.reply(14, channel="email", days=4)
        self.assertEqual(timeline.reply_habit(self.kontakt), "Svarar oftast på sms, dagtid")
        self.reply(15, channel="email", days=5)
        self.assertEqual(timeline.reply_habit(self.kontakt), "Svarar oftast dagtid")

    def test_stop_and_start_are_not_replies(self):
        self.reply(19)
        self.reply(20, days=1)
        self.reply(21, keyword="stop", days=2)
        self.reply(19, keyword="start", days=3)
        self.assertEqual(timeline.reply_habit(self.kontakt), "")

    def test_night_and_morning(self):
        for days in range(3):
            self.reply(23 if days else 2, days=days)
        self.assertEqual(timeline.reply_habit(self.kontakt), "Svarar oftast på sms, nattetid")
