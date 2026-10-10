"""
Leverantörernas namn når inte kunden (Giovanni 2026-10-10), inte heller från
rader som sparades innan texterna skrevs om:

  * ett klagomål hette "Klagomål · SES" på kontaktkortet, i tidslinjen och i
    personens export; nu consent.COMPLAINT_DETAIL, och gamla rader visas så
    (loggen själv skrivs aldrig om, den är beviset);
  * en pausnot för studsar sa "före AWS gräns på 5 %"; Leveranshälsan visar
    sparade noter genom health.shown_note;
  * adressen till mottagarna som spärrats hos e-posttjänsten hette
    ?visa=hoppades-over-ses_suppressed; nu reports.SKIP_SLUGS, och den gamla
    adressen fungerar ändå.

Vakten i apps/common/test_leverantorer.py håller texterna i koden rena.
"""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import consent as consents
from . import contacts as register
from . import reports
from .inbound import events
from .models import CHANNEL_EMAIL, ConsentLog, Contact, Recipient, Utskick
from .sending import health
from .test_s3_events import EventFixture, complaint

OLD_NOTE = (
    "4 % av de första 200 mejlen studsade. Vi pausar vid 4 %, före AWS gräns på 5 %. "
    "ADX har fått ett larm. De studsade adresserna är redan markerade."
)


class ComplaintDetailTests(EventFixture, TestCase):
    def complained(self):
        _u, (recipient,) = self.sent()
        events.apply(complaint(recipient))
        return Contact.objects.get(pk=recipient.contact_id)

    def assert_clean(self, kontakt):
        client = self.client_for(self.anna)
        card = client.get(reverse("flamingo:app_contact", args=[kontakt.pk]))
        self.assertEqual(card.status_code, 200)
        self.assertContains(card, consents.COMPLAINT_DETAIL)
        self.assertNotContains(card, "SES")
        exported = str(register.export_contact(kontakt))
        self.assertIn(consents.COMPLAINT_DETAIL, exported)
        self.assertNotIn("SES", exported)

    def test_a_new_complaint_is_described_without_the_provider(self):
        kontakt = self.complained()
        row = kontakt.consents.get(channel=CHANNEL_EMAIL)
        self.assertEqual(row.source_detail, consents.COMPLAINT_DETAIL)
        self.assert_clean(kontakt)

    def test_rows_saved_with_the_provider_name_are_shown_without_it(self):
        kontakt = self.complained()
        kontakt.consents.filter(source="complaint").update(source_detail="SES")
        ConsentLog.objects.filter(contact=kontakt, source="complaint").update(source_detail="SES")
        self.assert_clean(kontakt)
        # Beviset står kvar som det skrevs.
        self.assertTrue(ConsentLog.objects.filter(contact=kontakt, source_detail="SES").exists())


class PauseNoteTests(EventFixture, TestCase):
    def test_the_new_note_names_no_provider(self):
        verdict = health.Verdict(bounced=8, delivered=192, outcomes=200, sent=200)
        text = health.judge(verdict).text
        self.assertIn("före e-posttjänstens gräns på 5 %", text)
        self.assertNotIn("AWS", text)

    def test_an_old_note_is_shown_without_the_name(self):
        u = self.sending(1)
        Utskick.objects.filter(pk=u.pk).update(
            status=Utskick.Status.PAUSED_HEALTH,
            pause_reason=Utskick.PauseReason.BOUNCES,
            stats={"pause": {"note": OLD_NOTE}},
        )
        page = self.client_for(self.anna).get(reverse("flamingo:app_utskick_health"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Vi pausar vid 4 %, före e-posttjänstens gräns på 5 %.")
        self.assertNotContains(page, "AWS")

    def test_shown_note_leaves_other_notes_alone(self):
        self.assertEqual(health.shown_note(""), "")
        self.assertEqual(health.shown_note(health.BLOCKED_TEXT), health.BLOCKED_TEXT)


class SkippedViewSlugTests(SimpleTestCase):
    def test_the_suppressed_view_has_a_neutral_address(self):
        suppressed = Recipient.SkipReason.SES_SUPPRESSED
        view = reports.skipped_view(suppressed)
        self.assertEqual(view, "hoppades-over-sparrad-hos-e-posttjansten")
        self.assertEqual(reports.skipped_reason(view), suppressed)
        self.assertEqual(reports.view_label(view), "Hoppades över: spärrad hos e-posttjänsten")
        self.assertEqual(
            str(reports.view_q(None, view)),
            str(reports.view_q(None, "hoppades-over-ses_suppressed")),
        )

    def test_old_addresses_and_other_reasons_still_work(self):
        self.assertEqual(
            reports.skipped_reason("hoppades-over-ses_suppressed"),
            Recipient.SkipReason.SES_SUPPRESSED,
        )
        self.assertEqual(reports.skipped_view("weekly_cap"), "hoppades-over-weekly_cap")
        self.assertEqual(reports.skipped_reason("hoppades-over-weekly_cap"), "weekly_cap")
        self.assertIsNone(reports.skipped_reason("hoppades-over-okand"))
        self.assertIsNone(reports.view_q(None, "hoppades-over-okand"))
        self.assertEqual(reports.view_label("hoppades-over-okand"), "")
