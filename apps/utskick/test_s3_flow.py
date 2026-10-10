"""Hela vägen genom S3 (README J S3, "Acceptance"), med de riktiga vyerna,
renderaren, ticken, transporten och köerna. Varje del har sina egna tester;
här prövas att delarna hänger ihop:

    SendFlowTests      redigeraren (rita/, spara/), Innehåll, Tid, Granska med
                       mejlets kontroller, bekräftelsen, ticken (frysningen med
                       ögonblicksbilden och länkarna, e-postslingan), SES
                       (FakeSes: rå MIME, List-Unsubscribe och ettklicket),
                       händelserna genom kön (levererat, studs), hälsan och
                       rapportens e-postrutor
    LinkFlowTests      klicket på klick.adx.se till landningssidan med ut och
                       förfrågan med spåret, pixeln (bara tracking_ok),
                       webbversionen med CSP, kalenderfilen och Dina val (/v/)
    UnsubscribeFlowTests
                       ettklicket ur List-Unsubscribe (POST utan CSRF), sidan
                       /a/ (GET ändrar inget) och mailto-avregistreringen
                       genom inkorgskön
    ReplyFlowTests     ett svar på mejlet genom inkorgskön och hinken, tråden i
                       Inkorgen och svaret från Inkorgen med In-Reply-To
    TimelineTests      kontaktkortets tidslinje och Senast för mejlen

Inget når nätet: SES är transport.FakeSes, SQS och S3 är attrapper genom
aws.client. Klockan är den riktiga.
"""

import json
import re
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.exports import landing_page_url
from apps.flamingo.models import Fact, Lead

from . import consent as consents
from . import keys, timeline, tokens
from .app_views.contacts import last_text
from .email import mime, transport
from .models import (
    CHANNEL_EMAIL,
    Click,
    Consent,
    ContactList,
    Event,
    InboundMessage,
    Recipient,
    Suppression,
    Switchboard,
    Thread,
    ThreadMessage,
    TrackedLink,
    Utskick,
)
from .sending import tick
from .test_s1_lp import LpFixture
from .test_s2_foundation import LINK_SETTINGS
from .test_s3_inbound_email import BUCKET, FakeS3, raw_mail
from .test_s3_queues import FakeSqs
from .test_s3_transport import EVENTS_URL, INBOUND_URL
from .testing import make_contact

FLOW = {
    **LINK_SETTINGS,
    "UTSKICK_EMAIL_LIVE": True,
    "UTSKICK_SQS_EVENTS_URL": EVENTS_URL,
    "UTSKICK_SQS_INBOUND_URL": INBOUND_URL,
    "UTSKICK_SES_INBOUND_BUCKET": BUCKET,
    "UTSKICK_EMAIL_PER_SECOND": 1000,
    "UTSKICK_ADX_MONTHLY_MAIL_CAP": 2000,
    "UTSKICK_ADX_MAIL_DOMAIN": "utskick.adx.se",
    "UTSKICK_REPLY_DOMAIN": "svar.utskick.adx.se",
    "SITE_BASE_URL": "https://adx.example",
    "INQUIRY_NOTIFICATION_EMAIL": "byran@adx.example",
}
KLICK = {"HTTP_HOST": "klick.adx.se"}
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
CLICK_RE = re.compile(r'href="(https://klick\.adx\.se/m/[A-Za-z0-9.]+)"')
PIXEL_RE = re.compile(r'src="(https://klick\.adx\.se/o/[A-Za-z0-9.]+\.gif)"')
WEB_RE = re.compile(r'href="(https://klick\.adx\.se/w/[A-Za-z0-9.]+)"')
CAL_RE = re.compile(r'href="(https://klick\.adx\.se/c/[A-Za-z0-9._]+\.ics)"')
PREF_RE = re.compile(r'href="(https://klick\.adx\.se/v/[A-Za-z0-9._-]+)"')
FN_RE = re.compile(r'name="fn" value="([^"]+)"')
RS = Recipient.Status
PEOPLE = (
    ("Anna", "anna.ek@hemma.example", True),
    ("Bo", "bo.ek@hemma.example", False),
    ("Cilla", "cilla.ek@hemma.example", False),
)


def path_of(url):
    return urlsplit(url).path


class FakeAws:
    """aws.client för flödet: SQS och S3 som attrapper (SES går genom
    transport.FakeSes)."""

    def __init__(self):
        self.sqs = FakeSqs()
        self.s3 = FakeS3()

    def __call__(self, service, send=False):
        if service == "sqs":
            return self.sqs
        if service == "s3":
            return self.s3
        raise AssertionError(f'aws.client("{service}") ska inte anropas i flödet')


class S3FlowFixture(LpFixture):
    """Exempelrör med e-posten påslagen, adressen under Företaget, listan
    Kunder med tre kontakter som sagt ja till e-post (Anna med pixeln) och
    Flamingo-sidan Badrum Nacka."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Fact.objects.create(
            account=cls.account,
            key="adress",
            label="Adress",
            value="Exempelvägen 4, 123 45 Exempelstad",
            confirmed=True,
        )
        cls.kunder = ContactList.objects.create(account=cls.account, name="Kunder")
        cls.people = {}
        for first, email, tracking in PEOPLE:
            kontakt = make_contact(cls.account, first_name=first, last_name="Ek", email=email)
            consents.set_status(
                kontakt,
                CHANNEL_EMAIL,
                consents.YES,
                source=Consent.Source.MANUAL,
                evidence="kassan, 2025",
                tracking_ok=tracking,
            )
            cls.kunder.memberships.create(contact=kontakt)
            cls.people[first] = kontakt
        cls.settings.open_tracking = True
        cls.settings.save(update_fields=["open_tracking"])
        now = timezone.now()
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK,
            defaults={
                "email_enabled": True,
                "email_ready_at": now,
                "doi_ready_at": now,
                "links_ready_at": now,
            },
        )

    def setUp(self):
        super().setUp()
        overridden = override_settings(**FLOW)
        overridden.enable()
        self.addCleanup(overridden.disable)
        self.aws = FakeAws()
        patcher = mock.patch("apps.utskick.aws.client", side_effect=self.aws)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch("apps.utskick.sending.email.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        for target, kwargs in (
            ("apps.assistant.llm.is_configured", {"return_value": False}),
            ("apps.assistant.llm.call", {"side_effect": AssertionError("AI ska inte anropas")}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.customer_client = self.client_for(self.anna)
        self.ses = None

    # -- kunden bygger mejlet -----------------------------------------------

    def step(self, utskick, name):
        return reverse("flamingo:app_utskick_step", args=[utskick.pk, name])

    def post_json(self, name, utskick, data):
        response = self.customer_client.post(
            reverse(name, args=[utskick.pk]),
            data=json.dumps(data),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.json()

    def block(self, utskick, type_key, **fields):
        block = self.post_json("flamingo:app_brev_render_block", utskick, {"type": type_key})[
            "block"
        ]
        block["versions"][-1]["fields"].update(fields)
        return block

    def build_mail(self, utskick):
        """Redigeraren som kunden använder den: blocken från rita/, sedan
        spara/ med ämnesraden, försidestexten och färgen."""
        day = (timezone.localdate() + timedelta(days=14)).isoformat()
        blocks = [
            self.block(
                utskick,
                "hero",
                title="Dags för badrummet",
                lead="Vi renoverar badrum i Nacka och Värmdö.",
                button_text="Boka ett besök",
                button_url=landing_page_url(self.quote_page),
            ),
            self.block(
                utskick,
                "text",
                body="Hej {förnamn|du},\n\nnu har vi tider i november. "
                "Läs mer på [vår webbplats](https://exempelror.example/badrum).",
            ),
            self.block(
                utskick,
                "event",
                date=day,
                start="15:00",
                end="18:00",
                title="Öppet hus i butiken",
                place="Exempelvägen 4",
                calendar="yes",
            ),
        ]
        utskick.refresh_from_db()
        data = self.post_json(
            "flamingo:app_brev_save",
            utskick,
            {
                "rev": utskick.email_rev,
                "blocks": blocks,
                "subject": "Hej {förnamn|du}, dags för badrummet",
                "preheader": "Tider i november",
                "accent": "#1F7A4D",
            },
        )
        self.assertTrue(data.get("ok", True), data)
        utskick.refresh_from_db()
        self.assertEqual(len(utskick.email_doc["blocks"]), 3)
        return utskick

    def build_and_confirm(self):
        """Guiden: Mottagare, Kanal (bara e-post), redigeraren, Innehåll, Tid
        (nu), Granska och bekräftelsen."""
        utskick = self.draft_with_mail()
        return self.confirm(utskick)

    def draft_with_mail(self):
        """Guiden fram till ett färdigt mejl: Mottagare, Kanal (bara e-post),
        Innehåll och redigeraren."""
        client = self.customer_client
        client.post(reverse("flamingo:app_utskick_new"))
        utskick = Utskick.objects.get(account=self.account)
        client.post(
            self.step(utskick, "mottagare"),
            {"namn": "Badrum i november", "lists": [self.kunder.pk], "nasta": "kanal"},
        )
        response = client.post(
            self.step(utskick, "kanal"), {"syfte": "reklam", "kanal": "email_only"}
        )
        self.assertRedirects(response, self.step(utskick, "innehall"))
        utskick.refresh_from_db()
        self.assertEqual(utskick.channel_mode, Utskick.ChannelMode.EMAIL_ONLY)
        self.assertTrue(utskick.open_tracking)
        page = client.get(self.step(utskick, "innehall"))
        self.assertContains(page, reverse("flamingo:app_brev", args=[utskick.pk]))
        editor = client.get(reverse("flamingo:app_brev", args=[utskick.pk]))
        self.assertEqual(editor.status_code, 200)
        self.build_mail(utskick)
        checks = client.get(
            reverse("flamingo:app_brev_checks", args=[utskick.pk]), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(checks.status_code, 200)
        preview = client.get(reverse("flamingo:app_brev_preview", args=[utskick.pk]))
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Content-Security-Policy", preview)
        return utskick

    def confirm(self, utskick):
        """Tid (nu), Granska och bekräftelsen."""
        client = self.customer_client
        response = client.post(self.step(utskick, "tid"), {"nar": "now"})
        self.assertRedirects(response, self.step(utskick, "granska"))
        page = client.get(self.step(utskick, "granska"))
        review = page.context["review"]
        self.assertFalse(review["blocking"], review["items"])
        self.assertContains(page, "3 mejl")
        response = client.post(
            reverse("flamingo:app_utskick_confirm", args=[utskick.pk]),
            {"nonce": page.context["nonce"]},
        )
        self.assertRedirects(response, reverse("flamingo:app_utskick", args=[utskick.pk]))
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SCHEDULED)
        self.assertEqual(utskick.confirm_summary["email"], 3)
        return utskick

    def notification(self, to_address, message_id, sender="cilla.ek@hemma.example"):
        return {
            "notificationType": "Received",
            "mail": {
                "timestamp": "2026-10-10T07:00:00.000Z",
                "source": sender,
                "messageId": message_id,
                "headers": [],
                "commonHeaders": {
                    "from": [sender],
                    "subject": "avregistrera",
                    "messageId": f"<{message_id}@mail.example>",
                },
            },
            "receipt": {
                "recipients": [to_address],
                "spamVerdict": {"status": "PASS"},
                "virusVerdict": {"status": "PASS"},
                "spfVerdict": {"status": "PASS"},
                "dkimVerdict": {"status": "PASS"},
                "dmarcVerdict": {"status": "PASS"},
                "action": {"type": "S3", "bucketName": BUCKET, "objectKey": f"in/{message_id}"},
            },
        }

    # -- ticken ----------------------------------------------------------------

    def run_tick(self):
        with self.captureOnCommitCallbacks(execute=True):
            return tick.run(now=timezone.now(), budget=30)

    def sent_utskick(self):
        """Bekräftat, fryst och skickat genom FakeSes. {förnamn: (mottagare,
        mejlet)}."""
        utskick = self.build_and_confirm()
        with transport.FakeSes() as ses:
            for _ in range(5):
                self.run_tick()
                utskick.refresh_from_db()
                if not utskick.recipients.filter(status__in=(RS.QUEUED, RS.SENDING)).exists():
                    break
        self.ses = ses
        self.assertIn(
            utskick.status, (Utskick.Status.SENDING, Utskick.Status.SENT), utskick.pause_reason
        )
        mails = {str(message["To"]): message for message in ses.messages}
        out = {}
        for first, kontakt in self.people.items():
            recipient = utskick.recipients.get(contact=kontakt)
            self.assertEqual(recipient.status, RS.SENT, first)
            out[first] = (recipient, mails[kontakt.email])
        return utskick, out

    def html(self, message):
        return mime.text_part(message, "html")

    def event(self, kind, recipient, **section):
        tags = {
            "a": [str(self.account.pk)],
            "u": [str(recipient.utskick_id)],
            "r": [str(recipient.pk)],
            "k": ["utskick"],
        }
        body = {
            "eventType": kind,
            "mail": {
                "timestamp": "2026-10-10T10:00:00.000Z",
                "messageId": recipient.ses_message_id,
                "destination": [recipient.address],
                "tags": tags,
            },
        }
        body.update(section)
        self.aws.sqs.put(EVENTS_URL, body)


# ---------------------------------------------------------------------------
# Från redigeraren till rapporten
# ---------------------------------------------------------------------------


class SendFlowTests(S3FlowFixture, TestCase):
    def test_editor_to_ses_to_events_to_the_report(self):
        utskick, mails = self.sent_utskick()
        # Frysningen: ögonblicksbilden och en TrackedLink per webblänk.
        self.assertTrue(utskick.email_snapshot.get("blocks"))
        links = list(TrackedLink.objects.filter(utskick=utskick).order_by("pk"))
        kinds = sorted(link.kind for link in links)
        self.assertEqual(kinds, ["external", "lp"])
        self.assertEqual(len(utskick.email_snapshot["links"]), 2)
        # Mejlet: rå MIME med rätt huvuden, konfigurationssetet och taggarna.
        recipient, message = mails["Anna"]
        self.assertEqual(str(message["Subject"]), "Hej Anna, dags för badrummet")
        self.assertEqual(message["From"].addresses[0].addr_spec, "exempelror@utskick.adx.se")
        self.assertEqual(
            str(message["Reply-To"]),
            tokens.reply_address(tokens.REPLY, self.account.pk, recipient.pk),
        )
        https, mailto = [p.strip(" <>") for p in str(message["List-Unsubscribe"]).split(",")]
        self.assertTrue(https.startswith("https://klick.adx.se/a/"))
        self.assertTrue(mailto.startswith("mailto:s+u"))
        self.assertEqual(str(message["List-Unsubscribe-Post"]), "List-Unsubscribe=One-Click")
        self.assertEqual(self.ses.calls[0]["ConfigurationSetName"], "adx-utskick")
        html = self.html(message)
        self.assertIn('width="560"', html)
        self.assertIn("Dags för badrummet", html)
        self.assertNotIn("{förnamn", html)
        # Varje länk till en webbplats går genom klick.adx.se; pixeln bara hos Anna.
        self.assertEqual(len(CLICK_RE.findall(html)), 2)
        self.assertNotIn(landing_page_url(self.quote_page), html)
        self.assertEqual(len(PIXEL_RE.findall(html)), 1)
        self.assertEqual(PIXEL_RE.findall(self.html(mails["Bo"][1])), [])
        text = mime.text_part(message, "plain")
        self.assertIn("Dags för badrummet", text)
        # Händelserna genom kön: två levererade och en studs.
        anna, bo, cilla = (mails[n][0] for n in ("Anna", "Bo", "Cilla"))
        for recipient in (anna, bo):
            self.event(
                "Delivery",
                recipient,
                delivery={"timestamp": "2026-10-10T10:00:02.000Z", "recipients": []},
            )
        self.event(
            "Bounce",
            cilla,
            bounce={
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{"emailAddress": cilla.address}],
            },
        )
        summary = self.run_tick()
        self.assertTrue(summary)
        self.assertEqual(self.aws.sqs.queues.get(EVENTS_URL), [])
        statuses = dict(utskick.recipients.values_list("contact__first_name", "status"))
        self.assertEqual(statuses, {"Anna": RS.DELIVERED, "Bo": RS.DELIVERED, "Cilla": RS.BOUNCED})
        cilla_contact = self.people["Cilla"]
        cilla_contact.refresh_from_db()
        self.assertEqual(cilla_contact.email_state, "bounced")
        self.assertTrue(
            Suppression.objects.filter(
                account=self.account,
                channel=CHANNEL_EMAIL,
                value_hash=keys.value_hash(CHANNEL_EMAIL, cilla_contact.email),
                reason=Suppression.Reason.BOUNCE,
            ).exists()
        )
        # Samma händelse en gång till gör ingenting (kvittot).
        self.event(
            "Bounce",
            cilla,
            bounce={
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{"emailAddress": cilla.address}],
            },
        )
        self.run_tick()
        self.assertEqual(Suppression.objects.filter(account=self.account).count(), 1)
        # Rapporten: e-postens rutor.
        report = self.customer_client.get(reverse("flamingo:app_utskick", args=[utskick.pk]))
        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "Levererade")
        self.assertContains(report, "Studsar")
        self.assertContains(report, "Öppnat (indikation)")
        # Leveranshälsan: studsen syns, utan adressen i klartext.
        health = self.customer_client.get(reverse("flamingo:app_utskick_health"))
        self.assertEqual(health.status_code, 200)
        self.assertNotContains(health, cilla_contact.email)
        # Ticken avslutar utskicket när alla har ett utfall.
        tick.finish(timezone.now() + timedelta(minutes=5), only=utskick.pk)
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SENT)

    def test_the_agency_overview_shows_the_email_part(self):
        self.sent_utskick()
        staff = Client()
        staff.force_login(self.staff)
        page = staff.get(reverse("manage:utskick_overview"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Provmejl till mig")
        self.assertContains(page, "E-postutskick är påslagna")


# ---------------------------------------------------------------------------
# Länkarna i mejlet (klick.adx.se)
# ---------------------------------------------------------------------------


class LinkFlowTests(S3FlowFixture, TestCase):
    def test_click_to_the_landing_page_and_a_lead_with_the_trail(self):
        utskick, mails = self.sent_utskick()
        recipient, message = mails["Anna"]
        lp_link = TrackedLink.objects.get(utskick=utskick, kind="lp")
        urls = CLICK_RE.findall(self.html(message))
        targets = {}
        for url in urls:
            ref = tokens.read_email_click(path_of(url).rsplit("/", 1)[1])
            self.assertEqual(ref.recipient_id, recipient.pk)
            targets[ref.link_id] = url
        url = targets[lp_link.pk]
        # HEAD ger målet utan ut och räknas inte.
        head = Client().head(path_of(url), **KLICK)
        self.assertEqual(head.status_code, 302)
        self.assertNotIn("ut=", head["Location"])
        # Förhandsvisningar räknas men sparas inte.
        Client().get(path_of(url), HTTP_USER_AGENT="WhatsApp/2.23", **KLICK)
        self.assertFalse(Click.objects.exists())
        response = Client().get(path_of(url), HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Set-Cookie", response)
        location = urlsplit(response["Location"])
        query = parse_qs(location.query)
        self.assertEqual(query["utm_medium"], ["email"])
        self.assertEqual(query["utm_campaign"], [f"utskick-{utskick.pk}"])
        click = Click.objects.get()
        self.assertEqual(click.channel, Click.Channel.EMAIL)
        self.assertEqual(click.kind, Click.Kind.HUMAN)
        self.assertEqual(click.contact, self.people["Anna"])
        self.assertEqual(query["ut"], [tokens.ut_token(click.pk)])
        recipient.refresh_from_db()
        self.assertEqual(recipient.click_count, 1)
        # Landningssidan med ut och en förfrågan med spåret.
        page = Client().get(location.path, {"ut": query["ut"][0]})
        self.assertEqual(page.status_code, 200)
        Client().post(
            location.path,
            {
                "name": "Anna Ek",
                "phone": "0701740699",
                "email": "anna.ek@hemma.example",
                "q_storlek": "6",
                "ut": query["ut"][0],
            },
        )
        lead = Lead.objects.get(account=self.account, utskick=utskick)
        self.assertEqual(lead.attribution["channel"], "email")
        self.assertEqual(lead.contact, self.people["Anna"])
        self.assertFalse(lead.can_send_to_google)
        # Ett klick på en annan mottagares länk med fel utskick: gått ut.
        other = Utskick.objects.create(account=self.account, name="Annat")
        bad = tokens.email_click_token(recipient.pk, lp_link.pk)
        Recipient.objects.filter(pk=recipient.pk).update(utskick=other)
        gone = Client().get(f"/m/{bad}", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(gone.status_code, 404)

    def test_the_pixel_counts_only_where_it_was_sent(self):
        utskick, mails = self.sent_utskick()
        anna, message = mails["Anna"]
        bo = mails["Bo"][0]
        pixel = PIXEL_RE.findall(self.html(message))[0]
        self.assertEqual(Client().head(path_of(pixel), **KLICK).status_code, 200)
        anna.refresh_from_db()
        self.assertIsNone(anna.opened_at)
        response = Client().get(path_of(pixel), **KLICK)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/gif")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Set-Cookie", response)
        Client().get(path_of(pixel), **KLICK)
        anna.refresh_from_db()
        self.assertIsNotNone(anna.opened_at)
        self.assertEqual(Event.objects.filter(kind=Event.OPENED).count(), 1)
        # Bo fick ingen pixel: en gissad token räknar ändå inget.
        guessed = f"/o/{tokens.pixel_token(bo.pk)}.gif"
        self.assertEqual(Client().get(guessed, **KLICK).status_code, 200)
        bo.refresh_from_db()
        self.assertIsNone(bo.opened_at)
        # En förfalskad token: 404.
        self.assertEqual(Client().get("/o/AAAAAAA.zzzzzzzz.gif", **KLICK).status_code, 404)
        # Kortet: "Öppnade (indikation)".
        titles = [item.title for item in timeline.for_contact(self.people["Anna"]).items]
        self.assertIn("Öppnade (indikation)", titles)
        report = self.customer_client.get(reverse("flamingo:app_utskick", args=[utskick.pk]))
        self.assertContains(report, "Öppnat (indikation)")

    def test_web_view_calendar_and_preferences(self):
        utskick, mails = self.sent_utskick()
        recipient, message = mails["Anna"]
        html = self.html(message)
        web = WEB_RE.findall(html)[0]
        response = Client().get(path_of(web), **KLICK)
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertContains(response, "Dags för badrummet")
        self.assertNotContains(response, "/o/")
        calendar = CAL_RE.findall(html)[0]
        response = Client().get(path_of(calendar), **KLICK)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/calendar; charset=utf-8")
        body = response.content.decode()
        self.assertIn("BEGIN:VEVENT", body)
        self.assertIn("Öppet hus i butiken", body)
        self.assertNotIn("Set-Cookie", response)
        # Dina val (/v/): sidan, och att stänga av e-posten sätter declined.
        preferences = PREF_RE.findall(html)[0]
        client = Client(enforce_csrf_checks=True)
        page = client.get(path_of(preferences), **KLICK)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Vad vill du få från Exempelrör?")
        self.assertContains(page, "E-post med erbjudanden")
        self.assertNotContains(page, "anna.ek@hemma.example")
        self.assertNotContains(page, "Anna")
        fn = FN_RE.search(page.content.decode()).group(1)
        response = client.post(
            path_of(preferences), {"fn": fn, "action": "spara"}, HTTP_ORIGIN="null", **KLICK
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("Set-Cookie", response)
        consent = Consent.objects.get(contact=self.people["Anna"], channel=CHANNEL_EMAIL)
        self.assertEqual(consent.status, consents.DECLINED)
        # Ingen spärr: information går fortfarande fram (H.6).
        self.assertFalse(Suppression.objects.filter(account=self.account).exists())


# ---------------------------------------------------------------------------
# Avregistreringen
# ---------------------------------------------------------------------------


class UnsubscribeFlowTests(S3FlowFixture, TestCase):
    def test_one_click_from_the_header(self):
        utskick, mails = self.sent_utskick()
        recipient, message = mails["Bo"]
        https = str(message["List-Unsubscribe"]).split(",")[0].strip(" <>")
        client = Client(enforce_csrf_checks=True)
        # Ett GET (förhandsvisning, skanner) ändrar ingenting.
        page = client.get(path_of(https), **KLICK)
        self.assertEqual(page.status_code, 200)
        self.assertFalse(Suppression.objects.exists())
        response = client.post(
            path_of(https),
            data="List-Unsubscribe=One-Click",
            content_type="application/x-www-form-urlencoded",
            HTTP_ORIGIN="null",
            **KLICK,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertNotIn("Set-Cookie", response)
        row = Suppression.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(row.reason, Suppression.Reason.LIST_UNSUB)
        self.assertEqual(row.utskick_id, utskick.pk)
        consent = Consent.objects.get(contact=self.people["Bo"], channel=CHANNEL_EMAIL)
        self.assertEqual(consent.status, consents.UNSUBSCRIBED)
        recipient.refresh_from_db()
        self.assertIsNotNone(recipient.stopped_at)

    def test_mailto_unsubscribe_through_the_inbound_queue(self):
        utskick, mails = self.sent_utskick()
        recipient, message = mails["Cilla"]
        mailto = str(message["List-Unsubscribe"]).split(",")[1].strip(" <>")
        address = mailto.removeprefix("mailto:").split("?", 1)[0]
        self.aws.sqs.put(INBOUND_URL, self.notification(address, "ses-unsub-1"))
        self.run_tick()
        consent = Consent.objects.get(contact=self.people["Cilla"], channel=CHANNEL_EMAIL)
        self.assertEqual(consent.status, consents.UNSUBSCRIBED)
        self.assertTrue(
            Suppression.objects.filter(account=self.account, channel=CHANNEL_EMAIL).exists()
        )
        self.assertFalse(Thread.objects.exists())


# ---------------------------------------------------------------------------
# Svar på mejlet
# ---------------------------------------------------------------------------


class ReplyFlowTests(S3FlowFixture, TestCase):
    def test_a_reply_lands_in_the_inbox_and_is_answered_by_email(self):
        utskick, mails = self.sent_utskick()
        recipient, message = mails["Anna"]
        reply_to = str(message["Reply-To"])
        key = "in/ses-reply-1"
        self.aws.s3.put(
            key,
            raw_mail(
                "Har ni tid en tisdag förmiddag?\n\n> Dags för badrummet",
                frm="Anna Ek <anna.ek@hemma.example>",
                to=reply_to,
                subject="Sv: Hej Anna, dags för badrummet",
                message_id="<CAreply1@mail.hemma.example>",
            ),
        )
        note = self.notification(reply_to, "ses-reply-1", sender="anna.ek@hemma.example")
        note["mail"]["commonHeaders"]["subject"] = "Sv: Hej Anna, dags för badrummet"
        self.aws.s3.objects[f"in/{note['mail']['messageId']}"] = self.aws.s3.objects[key]
        self.aws.sqs.put(INBOUND_URL, note)
        self.run_tick()
        row = InboundMessage.objects.get(channel=CHANNEL_EMAIL)
        self.assertEqual(row.status, InboundMessage.Status.ROUTED)
        thread = Thread.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(thread.contact, self.people["Anna"])
        self.assertEqual(thread.utskick, utskick)
        incoming = ThreadMessage.objects.get(thread=thread, direction=ThreadMessage.Direction.IN)
        self.assertIn("tisdag förmiddag", incoming.body)
        self.assertNotIn("> Dags", incoming.body)
        recipient.refresh_from_db()
        self.assertIsNotNone(recipient.replied_at)
        self.assertIn(f"in/{note['mail']['messageId']}", [k for _b, k in self.aws.s3.deleted])
        # Inkorgen: E-postsvar, och svaret med mejl därifrån.
        inbox = self.customer_client.get(reverse("flamingo:app_inbox"), {"typ": "e-postsvar"})
        self.assertContains(inbox, "E-postsvar")
        lead = thread.lead
        with transport.FakeSes() as ses:
            response = self.customer_client.post(
                reverse("flamingo:app_lead_reply", args=[lead.pk]),
                {"text": "Tisdag 09.00 passar bra. Välkommen."},
            )
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(len(ses.messages), 1, response.content[:300])
        answer = ses.messages[0]
        self.assertEqual(str(answer["To"]), "anna.ek@hemma.example")
        self.assertEqual(str(answer["In-Reply-To"]), "<CAreply1@mail.hemma.example>")
        self.assertEqual(
            str(answer["Reply-To"]),
            tokens.reply_address(tokens.THREAD, self.account.pk, thread.pk),
        )
        self.assertTrue(str(answer["Subject"]).startswith("Sv: "))
        self.assertEqual(ses.tags().get("k"), "reply")
        self.assertTrue(
            ThreadMessage.objects.filter(
                thread=thread, direction="out", body__startswith="Tisdag 09.00"
            ).exists()
        )

    def test_a_reply_to_the_test_mail_reaches_the_inbox_by_the_sender(self):
        """Testmejlet (F.8) har en svarsadress utan mottagare (NO_RECIPIENT):
        svaret routas på kontot i token och avsändaren (G.3)."""
        utskick = self.draft_with_mail()
        with transport.FakeSes() as ses:
            response = self.customer_client.post(
                reverse("flamingo:app_utskick_test", args=[utskick.pk]),
                {"kanal": "epost", "till": "mig"},
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(len(ses.messages), 1)
        test_mail = ses.messages[0]
        self.assertTrue(str(test_mail["Subject"]).startswith("Test: "))
        self.assertIsNone(test_mail["List-Unsubscribe"])
        self.assertEqual(str(test_mail["To"]), self.anna.email)
        reply_to = str(test_mail["Reply-To"])
        self.assertTrue(reply_to.startswith("s+r"))
        note = self.notification(reply_to, "ses-test-reply", sender=self.anna.email)
        self.aws.s3.put(
            f"in/{note['mail']['messageId']}",
            raw_mail("Ser bra ut.", frm=self.anna.email, to=reply_to, subject="Sv: Test"),
        )
        self.aws.sqs.put(INBOUND_URL, note)
        self.run_tick()
        thread = Thread.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(thread.address, self.anna.email)
        self.assertIsNone(thread.contact)
        self.assertEqual(
            ThreadMessage.objects.get(thread=thread, direction="in").body, "Ser bra ut."
        )


# ---------------------------------------------------------------------------
# Kontaktkortet
# ---------------------------------------------------------------------------


class TimelineTests(S3FlowFixture, TestCase):
    def test_bounce_and_delivery_on_the_card_and_the_list(self):
        utskick, mails = self.sent_utskick()
        cilla = mails["Cilla"][0]
        self.event(
            "Bounce",
            cilla,
            bounce={
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{"emailAddress": cilla.address}],
            },
        )
        self.run_tick()
        kontakt = self.people["Cilla"]
        kontakt.refresh_from_db()
        self.assertTrue(last_text(kontakt).startswith("Adressen finns inte, "))
        items = timeline.for_contact(kontakt).items
        details = [item.detail for item in items if item.kind == "utskick"]
        self.assertIn("Studsade: adressen finns inte", details)
        page = self.customer_client.get(reverse("flamingo:app_contact", args=[kontakt.pk]))
        self.assertContains(page, "Studsade: adressen finns inte")
