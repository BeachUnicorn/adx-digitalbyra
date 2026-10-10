"""Mejlens sidor på klick.adx.se som integrationen byggde (README E.1, E.3,
E.5, F.1 element 14, H.5, D.7): klicket (/m/), Dina val (/v/), pixeln (/o/)
och kalenderfilen (/c/). /a/ och /w/ prövas i test_s3_one_click och
test_s3_inbound_email; hela vägen i test_s3_flow.

    EmailClickTests        token, mottagaren, testmejlet, borttagen mottagare,
                           nekad värd, HEAD, bottar, missarna
    EmailPreferenceTests   sidan, nonce, stäng av, avregistrera från allt
                           (med och utan kontakt), inga kakor
    PixelTests             bara mottagare med pixeln, en gång, aldrig före
                           skickat, HEAD
    CalendarTests          blocket, fel block, kalendern avslagen

Alla förfrågningar går till klick.adx.se med Client(enforce_csrf_checks=True)
och HTTP_ORIGIN="null", och inget svar får sätta en kaka.
"""

import re
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from . import consent as consents
from . import keys, limits, link_views, tokens
from .models import (
    CHANNEL_EMAIL,
    AllowedHost,
    Click,
    Consent,
    ConsentLog,
    Counter,
    Event,
    Recipient,
    Suppression,
    TrackedLink,
    Utskick,
)
from .test_s3_foundation import LINK_SETTINGS
from .testing import UtskickFixture, make_contact

KLICK = {"HTTP_HOST": "klick.adx.se", "HTTP_ORIGIN": "null"}
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
FN_RE = re.compile(r'name="fn" value="([^"]+)"')
RS = Recipient.Status
BLOCK = "b_Ab12Cd34Ef56"


class LinkPageFixture(UtskickFixture):
    def setUp(self):
        super().setUp()
        overridden = override_settings(**LINK_SETTINGS)
        overridden.enable()
        self.addCleanup(overridden.disable)
        self.client = Client(enforce_csrf_checks=True)
        self.kontakt = make_contact(
            self.account, first_name="Anna", last_name="Lind", email="anna.lind@hemma.example"
        )
        consents.set_status(
            self.kontakt,
            CHANNEL_EMAIL,
            consents.YES,
            source=Consent.Source.MANUAL,
            evidence="kassan",
            tracking_ok=True,
        )
        self.utskick = Utskick.objects.create(
            account=self.account,
            name="Höstservice värmepump",
            channel_mode="email_only",
            status=Utskick.Status.SENDING,
            subject="Höstservice",
            open_tracking=True,
        )
        self.recipient = self.recipient_for(self.utskick)
        self.link = TrackedLink.objects.create(
            account=self.account,
            utskick=self.utskick,
            kind=TrackedLink.Kind.EXTERNAL,
            destination="https://exempelror.example/service",
            block_id=BLOCK,
            position=0,
        )

    def recipient_for(self, utskick, **kwargs):
        data = {
            "utskick": utskick,
            "contact": self.kontakt,
            "channel": CHANNEL_EMAIL,
            "address": self.kontakt.email,
            "status": RS.DELIVERED,
            "tracking_ok": True,
            "sent_at": timezone.now() - timedelta(hours=1),
            "delivered_at": timezone.now() - timedelta(hours=1),
        }
        data.update(kwargs)
        return Recipient.objects.create(**data)

    def get(self, path, **extra):
        response = self.client.get(path, **{**KLICK, **extra})
        self.assertNotIn("Set-Cookie", response)
        return response

    def post(self, path, data, **extra):
        response = self.client.post(path, data, **{**KLICK, **extra})
        self.assertNotIn("Set-Cookie", response)
        return response


# ---------------------------------------------------------------------------
# Klicket (/m/)
# ---------------------------------------------------------------------------


class EmailClickTests(LinkPageFixture, TestCase):
    def path(self, recipient_id=None, link_id=None):
        recipient_id = self.recipient.pk if recipient_id is None else recipient_id
        return f"/m/{tokens.email_click_token(recipient_id, link_id or self.link.pk)}"

    def test_a_human_click_is_counted_and_redirected_with_utm(self):
        response = self.get(self.path(), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("https://exempelror.example/service"))
        self.assertIn("utm_medium=email", response["Location"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])
        click = Click.objects.get()
        self.assertEqual(
            (click.channel, click.kind, click.contact_id),
            (Click.Channel.EMAIL, Click.Kind.HUMAN, self.kontakt.pk),
        )
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.click_count, 1)
        self.assertIsNotNone(self.recipient.first_clicked_at)

    def test_a_click_right_after_delivery_is_a_scanner(self):
        Recipient.objects.filter(pk=self.recipient.pk).update(delivered_at=timezone.now())
        self.get(self.path(), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(Click.objects.get().kind, Click.Kind.SCANNER)
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.click_count, 0)

    def test_a_preview_bot_is_counted_but_never_stored(self):
        response = self.get(self.path(), HTTP_USER_AGENT="Slackbot-LinkExpanding 1.0")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Click.objects.exists())
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.bot_hits, 1)

    def test_head_gives_the_target_and_counts_nothing(self):
        response = self.client.head(self.path(), **KLICK)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("utm_", response["Location"])
        self.assertFalse(Click.objects.exists())

    def test_the_test_mail_redirects_without_counting(self):
        response = self.get(self.path(recipient_id=0), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(response.status_code, 302)
        self.assertIn("utm_medium=email", response["Location"])
        self.assertFalse(Click.objects.exists())

    def test_a_recipient_removed_by_retention_still_reaches_the_target(self):
        path = self.path()
        self.recipient.delete()
        response = self.get(path, HTTP_USER_AGENT=IPHONE)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Click.objects.exists())

    def test_a_recipient_of_another_utskick_is_gone(self):
        other = Utskick.objects.create(account=self.account, name="Annat", status="sent")
        stranger = self.recipient_for(other)
        response = self.get(self.path(recipient_id=stranger.pk), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Länken har gått ut", status_code=404)
        self.assertFalse(Click.objects.exists())

    def test_a_refused_host_is_gone(self):
        self.link.destination = "https://annan-sajt.example/x"
        self.link.save(update_fields=["destination"])
        AllowedHost.objects.create(
            account=self.account, host="annan-sajt.example", status=AllowedHost.Status.REFUSED
        )
        response = self.get(self.path(), HTTP_USER_AGENT=IPHONE)
        self.assertEqual(response.status_code, 404)

    def test_a_forged_token_is_not_counted(self):
        # Mejlens token är signerade och går inte att gissa: missar räknas
        # inte, så att en delad adress aldrig spärras (säkerhetsgranskningen).
        forged = self.path()[:-1] + ("a" if not self.path().endswith("a") else "b")
        for _ in range(link_views.MISS_LIMIT + 1):
            self.assertEqual(self.get(forged).status_code, 404)
        self.assertFalse(Counter.objects.filter(scope="link_miss", count__gt=0).exists())
        self.assertEqual(self.get(self.path(), HTTP_USER_AGENT=IPHONE).status_code, 302)

    def test_only_on_klick_and_only_get(self):
        response = Client().get(self.path(), HTTP_HOST="k.adx.se")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.post(self.path(), {}).status_code, 404)


# ---------------------------------------------------------------------------
# Dina val (/v/)
# ---------------------------------------------------------------------------


class EmailPreferenceTests(LinkPageFixture, TestCase):
    def path(self, value_hash=None):
        value_hash = value_hash or keys.value_hash(CHANNEL_EMAIL, self.kontakt.email)
        return f"/v/{tokens.preference_token(self.account.pk, 'email', value_hash)}"

    def fn(self, page):
        return FN_RE.search(page.content.decode()).group(1)

    def test_the_page_shows_the_rows_masked(self):
        page = self.get(self.path())
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Vad vill du få från Exempelrör?")
        self.assertContains(page, "E-post med erbjudanden")
        self.assertContains(page, "a***@h***.example")
        self.assertContains(page, "Avregistrera mig från allt")
        self.assertNotContains(page, "Anna")
        self.assertNotContains(page, "anna.lind@hemma.example")
        self.assertNotContains(page, "csrfmiddlewaretoken")
        self.assertIn("no-store", page["Cache-Control"])

    def test_turning_email_off_declines_and_keeps_information(self):
        page = self.get(self.path())
        response = self.post(self.path(), {"fn": self.fn(page), "action": "spara"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("?klart=sparat"))
        consent = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_EMAIL)
        self.assertEqual(consent.status, consents.DECLINED)
        self.assertFalse(Suppression.objects.exists())
        self.assertEqual(
            ConsentLog.objects.filter(contact=self.kontakt).latest("at").source_detail,
            link_views.EMAIL_PREFERENCES_DETAIL,
        )
        done = self.get(response["Location"].removeprefix("http://klick.adx.se"))
        self.assertContains(done, "Dina val är sparade.")

    def test_a_stale_nonce_changes_nothing(self):
        response = self.post(self.path(), {"fn": "1.abc", "action": "allt"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Suppression.objects.exists())

    def test_unsubscribe_from_everything_suppresses_the_address(self):
        page = self.get(self.path())
        response = self.post(self.path(), {"fn": self.fn(page), "action": "allt"})
        self.assertEqual(response.status_code, 302)
        row = Suppression.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(row.reason, Suppression.Reason.PREFERENCE)
        consent = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_EMAIL)
        self.assertEqual(consent.status, consents.UNSUBSCRIBED)

    def test_without_a_contact_the_address_hash_is_suppressed(self):
        value_hash = keys.value_hash(CHANNEL_EMAIL, "borta@hemma.example")
        page = self.get(self.path(value_hash))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Du kan avregistrera den här adressen")
        response = self.post(self.path(value_hash), {"fn": self.fn(page), "action": "allt"})
        self.assertEqual(response.status_code, 302)
        row = Suppression.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(row.value_hash, value_hash)
        log = ConsentLog.objects.get(account=self.account, contact=None)
        self.assertEqual(log.channel, CHANNEL_EMAIL)
        self.assertEqual(log.source_detail, link_views.EMAIL_PREFERENCES_DETAIL)

    def test_works_when_utskick_is_off_for_the_account(self):
        self.settings.is_enabled = False
        self.settings.save(update_fields=["is_enabled"])
        self.assertEqual(self.get(self.path()).status_code, 200)

    def test_a_forged_token_is_gone(self):
        self.assertEqual(self.get(self.path()[:-2] + "zz").status_code, 404)


# ---------------------------------------------------------------------------
# Pixeln (/o/)
# ---------------------------------------------------------------------------


class PixelTests(LinkPageFixture, TestCase):
    def path(self, recipient=None):
        return f"/o/{tokens.pixel_token((recipient or self.recipient).pk)}.gif"

    def test_an_open_is_recorded_once(self):
        for _ in range(3):
            response = self.get(self.path())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "image/gif")
            self.assertEqual(response.content, link_views.PIXEL_GIF)
            self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.recipient.refresh_from_db()
        self.assertIsNotNone(self.recipient.opened_at)
        event = Event.objects.get(kind=Event.OPENED)
        self.assertEqual(
            (event.contact_id, event.utskick_id, event.recipient_id),
            (self.kontakt.pk, self.utskick.pk, self.recipient.pk),
        )

    def test_head_records_nothing(self):
        response = self.client.head(self.path(), **KLICK)
        self.assertEqual(response.status_code, 200)
        self.recipient.refresh_from_db()
        self.assertIsNone(self.recipient.opened_at)

    def test_only_recipients_that_carried_the_pixel(self):
        bo = make_contact(self.account, first_name="Bo", email="bo@hemma.example")
        cilla = make_contact(self.account, first_name="Cilla", email="cilla@hemma.example")
        no_consent = self.recipient_for(
            self.utskick, contact=bo, address=bo.email, tracking_ok=False
        )
        queued = self.recipient_for(
            self.utskick, contact=cilla, address=cilla.email, status=RS.QUEUED, sent_at=None
        )
        for recipient in (no_consent, queued):
            with self.subTest(recipient=recipient.pk):
                self.assertEqual(self.get(self.path(recipient)).status_code, 200)
                recipient.refresh_from_db()
                self.assertIsNone(recipient.opened_at)
        Utskick.objects.filter(pk=self.utskick.pk).update(open_tracking=False)
        self.get(self.path())
        self.recipient.refresh_from_db()
        self.assertIsNone(self.recipient.opened_at)
        self.assertFalse(Event.objects.filter(kind=Event.OPENED).exists())

    def test_a_forged_token_is_404_and_no_miss_is_counted(self):
        self.assertEqual(self.get("/o/AAAAAAA.zzzzzzzz.gif").status_code, 404)
        self.assertEqual(limits.count("link_miss", "", limits.hour_window()), 0)


# ---------------------------------------------------------------------------
# Kalendern (/c/)
# ---------------------------------------------------------------------------


class CalendarTests(LinkPageFixture, TestCase):
    def event_doc(self, calendar="yes"):
        day = (timezone.localdate() + timedelta(days=10)).isoformat()
        return {
            "blocks": [
                {
                    "id": BLOCK,
                    "type": "event",
                    "fields": {
                        "date": day,
                        "start": "15:00",
                        "end": "18:00",
                        "title": "Öppet hus",
                        "place": "Exempelvägen 4",
                        "calendar": calendar,
                    },
                }
            ],
            "company": {"name": "Exempelrör AB"},
        }

    def path(self, block_id=BLOCK):
        return f"/c/{tokens.calendar_token(self.utskick.pk, block_id)}.ics"

    def test_the_event_as_a_calendar_file(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(email_snapshot=self.event_doc())
        response = self.get(self.path())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/calendar; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        body = response.content.decode()
        self.assertIn("BEGIN:VEVENT", body)
        self.assertIn("Öppet hus", body)

    def test_another_block_or_no_calendar_is_gone(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(email_snapshot=self.event_doc("no"))
        self.assertEqual(self.get(self.path()).status_code, 404)
        self.assertEqual(self.get(self.path("b_Zz12Cd34Ef56")).status_code, 404)

    def test_a_forged_token_is_a_miss(self):
        self.assertEqual(self.get(self.path()[:-6] + "x.ics").status_code, 404)
