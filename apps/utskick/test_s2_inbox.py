"""
Svaren i Inkorgen (README C.2, G.1 punkt 8, G.2, H.4, I.4; J S2 test_s2_inbox).

    ListTests          svarsförfrågan i listan och badgen, en chipsrad med
                       antal, statusvalet, activity_at, "Klar", kanalerna
    DetailTests        tråden, räknarens attribut, Kontaktkort och Klar
    ReplyTests         svar med sms från svarsnumret och företagets namn sist,
                       byråns kryssruta, demot, STOPP, nödbromsen, främmande konto
    UnsubscribeTests   "Avregistrera från sms" skriver spärren och loggen med vem
    GdprTests          export och borttagning tar med trådarna och sms:en
"""

from datetime import timedelta

from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import RunSQL
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.app_views.inbox import channel
from apps.flamingo.models import Lead
from apps.sms.models import SmsMessage

from . import contacts, keys, threads
from .app_views import inbox_reply
from .models import (
    ConsentLog,
    DpaAcceptance,
    InboundMessage,
    Recipient,
    Suppression,
    Switchboard,
    Thread,
    ThreadMessage,
    Utskick,
    UtskickSettings,
)
from .test_s2_inbound import REPLY_NUMBER, InboundFixture
from .testing import PHONE_ANNA, PHONE_BO


class InboxFixture(InboundFixture):
    """Anna har svarat på Höstservice värmepump: en tråd med en ny förfrågan."""

    def setUp(self):
        super().setUp()
        Switchboard.objects.update(sms_enabled=True)
        self.app = self.client_for(self.anna)
        self.out_sms, self.recipient = self.send_out()
        self.inbound = self.handle("Har ni tid tisdag förmiddag?")
        self.thread = self.thread_of()
        self.lead = self.thread.lead

    def lead_url(self, lead=None):
        return reverse("flamingo:app_lead", args=[(lead or self.lead).pk])

    def reply(self, text, client=None, lead=None, **extra):
        url = reverse("flamingo:app_lead_reply", args=[(lead or self.lead).pk])
        return (client or self.app).post(url, {"text": text, **extra})

    def sends(self):
        return [c for c in self.fake.calls if c.get("dryrun") != "yes"]


class ListTests(InboxFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.form_lead = Lead.objects.create(
            account=self.account,
            name="Bo Berg",
            created_at=timezone.now() - timedelta(days=1),
            activity_at=timezone.now() - timedelta(days=1),
        )

    def test_the_reply_is_in_the_list_and_raises_the_badge(self):
        response = self.app.get(reverse("flamingo:app_inbox"))
        self.assertEqual(response.context["new_lead_count"], 2)
        html = response.content.decode()
        self.assertIn("Anna Lind", html)
        self.assertIn("Sms-svar", html)
        self.assertIn("Har ni tid tisdag förmiddag?", html)

    def test_one_chip_row_with_counts_and_a_status_select(self):
        response = self.app.get(reverse("flamingo:app_inbox"))
        types = {t["value"]: t["count"] for t in response.context["types"]}
        self.assertEqual(types, {"": 2, "forfragningar": 1, "sms-svar": 1, "avregistreringar": 0})
        html = response.content.decode()
        self.assertIn("?typ=sms-svar", html)
        self.assertIn('name="status"', html)
        self.assertIn(">Visa</button>", html)
        self.assertNotIn('aria-label="Visa förfrågningar med status"', html)
        self.assertNotIn("E-postsvar", html, "inga e-postsvar i S2: chipset visas inte")

    def test_without_utskick_the_inbox_is_unchanged(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        response = self.app.get(reverse("flamingo:app_inbox"))
        html = response.content.decode()
        self.assertIn('aria-label="Visa förfrågningar med status"', html)
        self.assertNotIn('name="typ"', html)
        self.assertEqual(response.context["types"], [])
        # Svaret finns kvar i listan och går att öppna, utan svarsruta.
        self.assertIn("Anna Lind", html)
        detail = self.app.get(self.lead_url()).content.decode()
        self.assertIn("Har ni tid tisdag förmiddag?", detail)
        self.assertNotIn('name="text"', detail)
        self.assertIn("Utskick är inte aktiverat för dig. Be ADX slå på det.", detail)

    def test_type_and_status_filters(self):
        def names(query):
            response = self.app.get(reverse("flamingo:app_inbox") + query)
            return [lead.pk for lead in response.context["leads"]]

        self.assertEqual(names("?typ=sms-svar"), [self.lead.pk])
        self.assertEqual(names("?typ=forfragningar"), [self.form_lead.pk])
        self.assertEqual(names("?typ=avregistreringar"), [])
        self.send_out(to=PHONE_BO, utskick=None)
        self.handle("STOPP", frm=PHONE_BO)
        stop_lead = Thread.objects.get(address=PHONE_BO).lead
        self.assertEqual(names("?typ=avregistreringar"), [stop_lead.pk])
        self.assertEqual(names("?typ=sms-svar&status=new"), [self.lead.pk])
        self.assertEqual(names("?typ=sms-svar&status=contacted"), [])
        self.assertEqual(names("?typ=okand"), [stop_lead.pk, self.lead.pk, self.form_lead.pk])

    def test_activity_at_orders_the_list(self):
        newer = Lead.objects.create(account=self.account, name="Cilla")
        self.assertEqual(
            [lead.pk for lead in self.app.get(reverse("flamingo:app_inbox")).context["leads"]],
            [newer.pk, self.lead.pk, self.form_lead.pk],
        )
        self.handle("Eller onsdag?", now=timezone.now() + timedelta(minutes=5))
        self.assertEqual(
            [lead.pk for lead in self.app.get(reverse("flamingo:app_inbox")).context["leads"]],
            [self.lead.pk, newer.pk, self.form_lead.pk],
        )

    def test_the_migration_backfills_activity_at(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("flamingo", "0017_svar_pa_utskick")]
        sql = [op.sql for op in migration.operations if isinstance(op, RunSQL)]
        self.assertIn("UPDATE flamingo_lead SET activity_at = created_at", sql)

    def test_a_handled_reply_reads_klar(self):
        Lead.objects.filter(pk=self.lead.pk).update(status=Lead.STATUS_CONTACTED)
        html = self.app.get(reverse("flamingo:app_inbox") + "?status=contacted").content.decode()
        self.assertIn("Klar ·", html)
        detail = self.app.get(self.lead_url())
        self.assertEqual(detail.context["lead_status"], "Klar")
        self.assertIn(("contacted", "Klar"), detail.context["statuses"])

    def test_channel_labels(self):
        self.assertEqual(channel(Lead.objects.get(pk=self.lead.pk)), "Sms-svar")
        Thread.objects.filter(pk=self.thread.pk).update(kind=Thread.Kind.STOP)
        self.assertEqual(channel(Lead.objects.get(pk=self.lead.pk)), "STOPP")
        Thread.objects.filter(pk=self.thread.pk).update(kind=Thread.Kind.REPLY, channel="email")
        self.assertEqual(channel(Lead.objects.get(pk=self.lead.pk)), "E-postsvar")
        via = Lead.objects.create(
            account=self.account,
            utskick=self.utskick,
            attribution={"name": "Höstservice värmepump"},
            utm={"utm_source": "flamingo"},
        )
        self.assertEqual(channel(via), "Utskick: Höstservice värmepump")
        bare = Lead.objects.create(account=self.account, utskick=self.utskick)
        self.assertEqual(channel(bare), "Utskick")
        self.assertEqual(channel(self.form_lead), "Formulär på sidan")

    def test_an_utskick_lead_never_goes_to_google(self):
        lead = Lead.objects.create(account=self.account, gclid="Cj0KCQ_abc")
        self.assertTrue(lead.can_send_to_google)
        lead.utskick = self.utskick
        self.assertFalse(lead.can_send_to_google)


class DetailTests(InboxFixture, TestCase):
    def test_the_thread_with_the_reply_box(self):
        response = self.app.get(self.lead_url())
        html = response.content.decode()
        self.assertIn("Hej Anna, dags för service hos Exempelrör.", html)
        self.assertIn("Utskick: Höstservice värmepump", html)
        self.assertIn("Har ni tid tisdag förmiddag?", html)
        self.assertIn("data-ut-sms ", html)
        self.assertIn('data-ut-sms-count="fl-th-count"', html)
        self.assertIn('data-ut-sms-suffix=" /Exempelrör"', html)
        self.assertIn("Skickas från 0766 86 00 46 · ", html)
        self.assertIn('id="fl-th-count">GSM-7 · 12 av 160 · 1 del<', html)
        self.assertIn(reverse("flamingo:app_contact", args=[self.kontakt.pk]), html)
        self.assertIn(reverse("flamingo:app_lead_unsubscribe", args=[self.lead.pk]), html)
        self.assertIn("css/flamingo-app-utskick-thread.css", html)
        self.assertIn("js/flamingo-app-utskick.js", html)
        self.assertIn(">Klar</button>", html)
        self.assertNotIn("Jag svarar som ADX", html)
        self.thread.refresh_from_db()
        self.assertFalse(self.thread.unread)

    def test_staff_sees_the_checkbox_and_leaves_it_unread(self):
        html = self.client_for(self.staff).get(self.lead_url()).content.decode()
        self.assertIn("Jag svarar som ADX åt Exempelrör.", html)
        self.assertIn("Skicka som ADX", html)
        self.thread.refresh_from_db()
        self.assertTrue(self.thread.unread)

    def test_klar_sets_the_reply_as_handled(self):
        response = self.app.post(self.lead_url(), {"status": "contacted"}, follow=True)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.STATUS_CONTACTED)
        self.assertContains(response, "Sparat: klar.")
        self.assertNotIn(">Klar</button>", response.content.decode())

    def test_the_flag_offers_the_unsubscribe(self):
        Thread.objects.filter(pk=self.thread.pk).update(looks_like_stop=True)
        html = self.app.get(self.lead_url()).content.decode()
        self.assertIn("Ser ut som en avregistrering.", html)

    def test_counter_rows_text(self):
        self.assertEqual(threads.counter_text(" /Exempelrör"), "GSM-7 · 12 av 160 · 1 del")
        self.assertEqual(
            threads.counter_text("a" * 161, ore=39), "GSM-7 · 161 av 306 · 2 delar · 0,78 kr"
        )
        self.assertTrue(threads.counter_text("Hej 😀").startswith("UCS-2 · "))
        self.assertEqual(inbox_reply.reply_number_text(), "0766 86 00 46")


class ReplyTests(InboxFixture, TestCase):
    def test_a_reply_goes_from_the_reply_number_with_the_name_last(self):
        response = self.reply("Tisdag 10.30 går bra. Ska jag boka in dig?")
        self.assertRedirects(response, self.lead_url() + "#svar", fetch_redirect_response=False)
        (send,) = self.sends()
        body = "Tisdag 10.30 går bra. Ska jag boka in dig? /Exempelrör"
        self.assertEqual(
            (send["from"], send["to"], send["message"]), (REPLY_NUMBER, PHONE_ANNA, body)
        )
        message = ThreadMessage.objects.get(direction="out", sent_by=self.anna)
        self.assertEqual(
            (message.body, message.status, message.sent_as_staff), (body, "sent", False)
        )
        sms = message.sms_message
        self.assertEqual((sms.source, sms.reference), ("reply", f"~t{message.pk}"))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.STATUS_CONTACTED)
        self.thread.refresh_from_db()
        self.assertIsNotNone(self.thread.last_out_at)
        html = self.app.get(self.lead_url()).content.decode()
        self.assertIn("Svaret är skickat.", html)
        self.assertIn("Svar från Anna", html)
        # Nästa svar från Anna hamnar i samma tråd (svaret är det senaste sms:et).
        inbound = self.handle("Ja tack")
        self.assertEqual(inbound.routed_via, f"sms:{sms.pk}")
        self.assertEqual(Thread.objects.count(), 1)

    def test_the_name_is_not_added_twice(self):
        self.reply("Hej, Exempelrör här. Tisdag går bra.")
        self.assertEqual(self.sends()[0]["message"], "Hej, Exempelrör här. Tisdag går bra.")

    def test_a_double_click_sends_once(self):
        self.reply("Tisdag går bra")
        self.reply("Tisdag går bra")
        self.assertEqual(len(self.sends()), 1)

    def test_empty_and_too_long_keep_the_text(self):
        response = self.reply("   ")
        self.assertContains(response, "Skriv ett svar.")
        text = "a" * 950
        response = self.reply(text)
        self.assertContains(response, threads.TOO_LONG_TEXT)
        self.assertContains(response, text)
        self.assertEqual(self.sends(), [])
        self.assertFalse(ThreadMessage.objects.filter(direction="out", sent_by=self.anna).exists())

    def test_staff_must_tick_the_box_and_is_logged(self):
        staff = self.client_for(self.staff)
        response = self.reply("Hej från ADX", client=staff)
        self.assertContains(response, inbox_reply.STAFF_REQUIRED_TEXT)
        self.assertEqual(self.sends(), [])
        self.reply("Hej från ADX", client=staff, staff_ok="1")
        message = ThreadMessage.objects.get(direction="out", sent_by=self.staff)
        self.assertTrue(message.sent_as_staff)
        html = self.app.get(self.lead_url()).content.decode()
        self.assertIn("Skickat av ADX (", html)
        self.assertIn("åt kunden", html)

    def test_the_demo_refuses(self):
        self.account.is_demo = True
        self.account.save(update_fields=["is_demo"])
        response = self.reply("Tisdag går bra")
        self.assertContains(response, "Demokontot skickar aldrig.")
        self.assertEqual(self.sends(), [])

    def test_a_stopped_number_is_refused(self):
        threads.unsubscribe(self.thread, actor=self.actor())
        response = self.reply("Tisdag går bra")
        self.assertContains(response, "Personen har svarat STOPP.")
        self.assertEqual(self.sends(), [])

    def test_the_breaker_refuses(self):
        Switchboard.objects.update(sms_paused_until=timezone.now() + timedelta(minutes=5))
        response = self.reply("Tisdag går bra")
        self.assertContains(response, "Sms kan inte skickas just nu. Försök igen om en stund.")
        self.assertEqual(self.sends(), [])

    def test_a_provider_failure_keeps_the_text_and_no_message(self):
        self.fake.fail_send = "46elks svarade 403"
        response = self.reply("Tisdag går bra")
        self.assertContains(response, threads.REPLY_ERRORS["provider_error"])
        self.assertContains(response, "Tisdag går bra</textarea>")
        self.assertFalse(ThreadMessage.objects.filter(direction="out", sent_by=self.anna).exists())

    def test_another_accounts_lead_and_utskick_off_are_404(self):
        foreign = Lead.objects.create(account=self.other_account, source=Lead.SOURCE_REPLY)
        Thread.objects.create(account=self.other_account, channel="sms", lead=foreign)
        for name in ("flamingo:app_lead_reply", "flamingo:app_lead_unsubscribe"):
            url = reverse(name, args=[foreign.pk])
            self.assertEqual(self.app.post(url, {"text": "x"}).status_code, 404)
            self.assertEqual(self.app.get(reverse(name, args=[self.lead.pk])).status_code, 405)
        plain = Lead.objects.create(account=self.account, name="Utan tråd")
        self.assertEqual(self.reply("x", lead=plain).status_code, 404)
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        self.assertEqual(self.reply("Tisdag").status_code, 404)
        self.assertEqual(self.sends(), [])

    def actor(self):
        from .access import Actor

        return Actor(user=self.anna, label="Anna Lindqvist")


class UnsubscribeTests(InboxFixture, TestCase):
    def unsubscribe(self, client=None, lead=None):
        url = reverse("flamingo:app_lead_unsubscribe", args=[(lead or self.lead).pk])
        return (client or self.app).post(url)

    def test_the_button_writes_the_suppression_and_the_log_with_the_user(self):
        queued = Recipient.objects.create(
            utskick=Utskick.objects.create(account=self.account, name="Nästa"),
            contact=self.kontakt,
            channel="sms",
            address=PHONE_ANNA,
        )
        response = self.unsubscribe()
        self.assertRedirects(response, self.lead_url() + "#svar", fetch_redirect_response=False)
        row = Suppression.objects.get(account=self.account, channel="sms")
        self.assertEqual((row.reason, row.utskick), ("reply", self.utskick))
        log = ConsentLog.objects.get(contact=self.kontakt, source="reply")
        self.assertEqual(
            (log.by_user, log.by_staff, log.new_status), (self.anna, False, "unsubscribed")
        )
        queued.refresh_from_db()
        self.assertEqual(queued.status, "skipped")
        html = self.app.get(self.lead_url()).content.decode()
        self.assertIn(inbox_reply.SUPPRESSED_NOTE, html)
        self.assertNotIn('name="text"', html)
        self.assertEqual(Suppression.objects.count(), 1)
        self.unsubscribe()
        self.assertEqual(ConsentLog.objects.filter(source="reply").count(), 1)

    def test_staff_unsubscribing_is_logged_as_staff(self):
        self.unsubscribe(client=self.client_for(self.staff))
        log = ConsentLog.objects.get(contact=self.kontakt, source="reply")
        self.assertEqual((log.by_user, log.by_staff), (self.staff, True))
        self.assertTrue(log.by_label.startswith("ADX ("))

    def test_a_thread_without_a_contact_logs_on_the_hash(self):
        DpaAcceptance.objects.filter(account=self.account).delete()
        self.send_out(to=PHONE_BO, utskick=None)
        self.handle("Hej", frm=PHONE_BO)
        thread = Thread.objects.get(address=PHONE_BO)
        self.assertIsNone(thread.contact)
        self.unsubscribe(lead=thread.lead)
        log = ConsentLog.objects.get(value_hash=keys.value_hash("sms", PHONE_BO))
        self.assertEqual((log.contact, log.by_user, log.source), (None, self.anna, "reply"))


class GdprTests(InboxFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.reply("Tisdag går bra")
        self.api_sms = SmsMessage.objects.create(
            account=self.sms_account, source="api", to=PHONE_ANNA, body="Från API:t", status="sent"
        )

    def test_the_export_has_threads_utskick_and_sms(self):
        data = contacts.export_contact(self.kontakt)
        self.assertEqual(data["utskick"][0]["utskick"], "Höstservice värmepump")
        texts = [m["text"] for m in data["svar"][0]["meddelanden"]]
        self.assertIn("Har ni tid tisdag förmiddag?", texts)
        self.assertIn("Tisdag går bra /Exempelrör", texts)
        sms_texts = [m["text"] for m in data["sms"]]
        self.assertIn("Tisdag går bra /Exempelrör", sms_texts)
        self.assertNotIn("Från API:t", sms_texts)

    def test_delete_removes_threads_and_blanks_what_must_stay(self):
        reply_sms = ThreadMessage.objects.get(direction="out", sent_by=self.anna).sms_message
        other_lead = Lead.objects.create(account=self.account, name="Anna", contact=self.kontakt)
        contacts.delete_contact(self.kontakt, delete_leads=False)
        self.assertFalse(Thread.objects.exists())
        self.assertFalse(ThreadMessage.objects.exists())
        self.assertFalse(Lead.objects.filter(source=Lead.SOURCE_REPLY).exists())
        self.assertTrue(Lead.objects.filter(pk=other_lead.pk).exists())
        self.recipient.refresh_from_db()
        self.assertEqual((self.recipient.contact, self.recipient.address), (None, ""))
        self.assertEqual(self.recipient.merge, {})
        for sms in (self.out_sms, reply_sms):
            sms.refresh_from_db()
            self.assertEqual((sms.to, sms.body), ("", ""))
            self.assertTrue(sms.reference or sms.source == "utskick")
        self.api_sms.refresh_from_db()
        self.assertEqual((self.api_sms.to, self.api_sms.body), (PHONE_ANNA, "Från API:t"))
        self.inbound.refresh_from_db()
        self.assertEqual(self.inbound.from_address, "")
        self.assertFalse(InboundMessage.objects.filter(from_address=PHONE_ANNA).exists())
