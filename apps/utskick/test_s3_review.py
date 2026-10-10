"""
Rättelserna efter granskningen av S3 (säkerhet, korrekthet och UX), en
klass per område:

    InboundRegexTests     inkommande mejl med 1 MB text, HTML och huvuden tolkas på under
                          en sekund (inga uttryck som backar), och citaten tas fortfarande bort
    UnverifiedFromTests   From gäller bara när SPF eller DKIM gick och DMARC inte föll:
                          avregistrering med mejl och svar utan mottagarrad
    OneClickLimitTests    en adress över missgränsen kan alltid avregistrera sig med ettklicket
    DomainReviewTests     demot når aldrig SES eller DNS, en borttagen identitet och DKIM
                          TEMPORARY_FAILURE, utkasten och nya utskick från den verifierade domänen
    SendingReviewTests    aldrig ett mejl utan den frysta ögonblicksbilden (och en ny frysning
                          vid bekräftelsen), egna sidor med # och ?, ett oklart svar stoppar
                          slingan, taken räknar failed med sent_at, SSLError är oklart
    ProbeResumeTests      bara byrån fortsätter efter ett prov som inte gick
    TickShareTests        sms och e-post delar ticken när båda väntar
    GuideReviewTests      Kanal, Tid och Granska för e-post, och mejlets texter

Inget når nätet: SES är transport.FakeSes eller test_s3_domains.FakeSesApi,
S3 är test_s3_inbound_email.FakeS3 och dnspython är mockad.
"""

import base64
import time
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.exports import landing_page_url
from apps.flamingo.models import Campaign, FlamingoAccount, Service

from . import limits, link_views, links, threads, tokens
from .access import Actor
from .app_views import utskick as utskick_views
from .email import checks as email_checks
from .email import domains, mime, style, transport
from .inbound import email as inbound_email
from .inbound import events
from .models import (
    Counter,
    InboundMessage,
    Recipient,
    SenderDomain,
    Suppression,
    Thread,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from .sending import email as email_loop
from .sending import health, state, tick
from .test_s3_domains import DomainFixture, dns_for
from .test_s3_events import bounce
from .test_s3_inbound_email import InboundFixture, raw_mail
from .test_s3_one_click import UnsubscribeFixture
from .test_s3_transport import STAFF, EmailFixture, outgoing
from .testing import UtskickFixture

RS = Recipient.Status
ANNA = Actor(label="Anna Lindqvist")
MB = 1_000_000


# ---------------------------------------------------------------------------
# Inkommande mejl: tiden och citaten
# ---------------------------------------------------------------------------


class InboundRegexTests(SimpleTestCase):
    def timed(self, fn, value):
        start = time.monotonic()
        result = fn(value)
        self.assertLess(time.monotonic() - start, 1.0, fn.__name__)
        return result

    def test_crafted_text_is_fast(self):
        for body in (
            " " * MB + "x",
            "*" * MB,
            "From:" + " " * MB,
            " *" * (MB // 2) + "Från: x",
            "Den 8 okt\n" * (MB // 10),
            "> " * (MB // 2),
            "_" * MB,
        ):
            self.timed(inbound_email.strip_quotes, body)

    def test_crafted_html_is_fast(self):
        for markup in (
            "<div class='x " * (MB // 14),
            "<head" * (MB // 5),
            "<head>" + " " * MB,
            "<li" * (MB // 3),
            "<br" + " " * MB,
            "<" + " " * MB,
            '<div class="' + 'gmail_quote"' * (MB // 12),
            "<span class='" + "gmail_quote'" * (MB // 12),
            "<div " + "x" * MB + "gmail_quote",
        ):
            self.timed(inbound_email.html_text, markup)

    def test_crafted_headers_are_fast(self):
        for value in ("a" * MB, "<" + "a" * MB, "a@" * (MB // 2), "x " * (MB // 2)):
            self.timed(inbound_email.address_in, value)

    def test_a_whole_mail_with_a_long_line_is_fast(self):
        body = base64.encodebytes(b" " * MB + b"x")
        raw = (
            b"From: Anna <anna@kund.example>\r\nTo: s+r1.x@svar.utskick.adx.se\r\n"
            b"Subject: Sv: Hej\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n" + body
        )
        message = mime.parse(raw)
        content = self.timed(inbound_email.content_of, message)
        self.assertEqual(content.from_address, "anna@kund.example")

    def test_the_quotes_are_still_found(self):
        html_text = inbound_email.html_text
        self.assertEqual(
            html_text(
                '<div dir="ltr">Tisdag passar.</div>'
                '<div class="gmail_quote"><div>Den tis 8 okt. skrev Exempelrör:</div></div>'
            ),
            "Tisdag passar.",
        )
        self.assertEqual(
            html_text(
                "<p>Ja tack.</p><div id='divRplyFwdMsg' dir='ltr'><b>Från:</b> Exempelrör</div>"
            ),
            "Ja tack.",
        )
        self.assertEqual(
            html_text('<p>Okej.</p><DIV CLASS="Moz-Cite-Prefix">Den 8 okt skrev X:</DIV>'),
            "Okej.",
        )
        self.assertEqual(html_text("<p>Okej.</p><blockquote>gammalt</blockquote>"), "Okej.")
        self.assertEqual(
            html_text(
                "<html><head><title>T</title><style>p{color:red}</style></head>"
                "<body><p>Hej</p></body></html>"
            ),
            "Hej",
        )
        # Markören i texten (inte i en div:s class eller id) är inget citat.
        self.assertEqual(
            html_text('<div class="intro">Ordet gmail_quote i texten.</div>'),
            "Ordet gmail_quote i texten.",
        )
        self.assertEqual(
            inbound_email.strip_quotes(
                "Ja, tisdag.\n\n**Från:** Exempelrör <hej@exempelror.example>\n"
                "**Skickat:** den 8 oktober 2026\n**Ämne:** Höstservice"
            ),
            "Ja, tisdag.",
        )


# ---------------------------------------------------------------------------
# Inkommande mejl: From utan mottagarrad
# ---------------------------------------------------------------------------


class UnverifiedFromTests(InboundFixture, TestCase):
    def mailto(self, object_id=None, **kwargs):
        address = self.reply_to(tokens.MAILTO, object_id=object_id)
        note = self.notification(recipients=[address], subject="avregistrera", **kwargs)
        return self.receive(note, raw=raw_mail(subject="avregistrera"))

    def test_the_verdicts(self):
        verified = inbound_email.from_verified
        self.assertTrue(verified({"spf": "PASS", "dkim": "FAIL", "dmarc": "GRAY"}))
        self.assertTrue(verified({"dkim": "PASS"}))
        self.assertFalse(verified({"spf": "PASS", "dkim": "PASS", "dmarc": "FAIL"}))
        self.assertFalse(verified({"spf": "FAIL", "dkim": "GRAY"}))
        self.assertFalse(verified({}))

    def test_a_forged_from_never_unsubscribes_anyone(self):
        pk = self.recipient.pk
        Recipient.objects.filter(pk=pk).delete()
        for verdicts in (
            {"spf": "FAIL", "dkim": "FAIL"},
            {"spf": "PASS", "dkim": "PASS", "dmarc": "FAIL"},
            {"spf": "GRAY", "dkim": "GRAY", "dmarc": "GRAY"},
        ):
            with self.subTest(**verdicts):
                row = self.mailto(object_id=pk, frm="Anna <anna@kund.example>", **verdicts)
                self.assertEqual(row.status, InboundMessage.Status.IGNORED)
                self.assertEqual(row.meta["reason"], "unverified_from")
        self.assertFalse(Suppression.objects.filter(account=self.account).exists())

    def test_a_verified_from_still_unsubscribes(self):
        pk = self.recipient.pk
        Recipient.objects.filter(pk=pk).delete()
        row = self.mailto(object_id=pk, spf="FAIL", dkim="PASS", dmarc="GRAY")
        self.assertEqual((row.status, row.meta["via"]), (InboundMessage.Status.STOP, "from"))

    def test_the_recipient_row_needs_no_verdicts(self):
        row = self.mailto(spf="FAIL", dkim="FAIL", dmarc="FAIL")
        self.assertEqual((row.status, row.meta["via"]), (InboundMessage.Status.STOP, "recipient"))

    def test_a_forged_reply_never_lands_on_a_contact(self):
        first = self.deliver()
        self.assertEqual(first.status, InboundMessage.Status.ROUTED)
        own = Thread.objects.get()
        self.assertEqual(own.contact_id, self.contact.pk)
        Recipient.objects.filter(pk=self.recipient.pk).delete()
        row = self.deliver(spf="FAIL", dkim="FAIL", dmarc="FAIL")
        self.assertEqual(row.status, InboundMessage.Status.ROUTED)
        self.assertTrue(row.meta["unverified_from"])
        self.assertIsNone(row.contact_id)
        forged = Thread.objects.exclude(pk=own.pk).get()
        self.assertIsNone(forged.contact_id)
        self.assertEqual(own.messages.count(), 1)
        note = threads.email_rows(forged)[0]
        self.assertEqual(note.note, "Avsändaren går inte att bekräfta: anna@kund.example")
        self.assertEqual(note.tone, "warn")

    def test_a_verified_reply_without_the_row_still_finds_the_contact(self):
        Recipient.objects.filter(pk=self.recipient.pk).delete()
        row = self.deliver()
        self.assertNotIn("unverified_from", row.meta)
        self.assertEqual(Thread.objects.get().contact_id, self.contact.pk)


# ---------------------------------------------------------------------------
# Ettklicket och missgränsen
# ---------------------------------------------------------------------------


class OneClickLimitTests(UnsubscribeFixture, TestCase):
    def test_one_click_works_from_an_address_over_the_miss_limit(self):
        from apps.flamingo.limits import ip_hash

        key = ip_hash("127.0.0.1")
        for _ in range(link_views.MISS_LIMIT + 1):
            limits.hit("link_miss", key, limits.hour_window(), 10**6)
        self.assertGreater(
            limits.count("link_miss", key, limits.hour_window()), link_views.MISS_LIMIT
        )
        response = self.one_click()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.suppression())

    def test_unknown_tokens_are_never_counted(self):
        garbage = "/a/" + "A" * 60
        for _ in range(link_views.MISS_LIMIT + 5):
            self.assertEqual(self.one_click(garbage).status_code, 404)
            self.assertEqual(
                self.client.get(garbage, **{"HTTP_HOST": "klick.adx.se"}).status_code, 404
            )
        self.assertFalse(Counter.objects.filter(scope="link_miss", count__gt=0).exists())
        self.assertEqual(self.one_click().status_code, 200)
        self.assertIsNotNone(self.suppression())


# ---------------------------------------------------------------------------
# Domänerna
# ---------------------------------------------------------------------------


class DomainReviewTests(DomainFixture, TestCase):
    def verify(self, row):
        self.ses.verified, self.ses.dkim = True, "SUCCESS"
        with dns_for(row.domain):
            row = domains.check(row)
        self.assertEqual(row.status, SenderDomain.Status.VERIFIED)
        return row

    def test_the_demo_never_reaches_ses_or_dns(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        with self.assertRaises(domains.DomainRefused) as refused:
            self.claim()
        self.assertEqual(refused.exception.text, domains.DEMO_TEXT)
        row = SenderDomain.objects.create(
            account=self.account,
            domain="exempelror.example",
            from_name="Exempelrör",
            ses_created=True,
            status=SenderDomain.Status.VERIFIED,
            verified_at=timezone.now(),
        )
        with dns_for("exempelror.example") as resolve:
            domains.check(row)
            self.assertEqual(domains.check_due(), {})
        resolve.assert_not_called()
        self.assertFalse(domains.delete_identity(row))
        self.assertEqual(self.ses.calls, [])

    def test_the_demo_domain_page_refuses_every_post(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        client = self.client_for(self.staff)
        url = reverse("flamingo:app_utskick_domain")
        with dns_for("exempelror.example") as resolve:
            for action in ("claim", "check", "help", "remove", "sender", "reply_send"):
                response = client.post(url, {"action": action, "domain": "exempelror.example"})
                self.assertRedirects(response, url, fetch_redirect_response=False)
        resolve.assert_not_called()
        self.assertEqual(self.ses.calls, [])
        self.assertFalse(SenderDomain.objects.filter(account=self.account).exists())
        self.assertEqual(mail.outbox, [])

    def test_a_deleted_identity_fails_the_domain_with_an_alert(self):
        row = self.verify(self.claim())

        def gone(EmailIdentity):  # noqa: N803 - boto3:s namn
            raise self.ses._error("NotFoundException", "GetEmailIdentity")

        self.ses.get_email_identity = gone
        mail.outbox.clear()
        with dns_for(row.domain):
            row = domains.check(row)
        self.assertEqual(row.status, SenderDomain.Status.FAILED)
        self.assertTrue(row.ses_snapshot["missing"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("identiteten finns inte hos SES", mail.outbox[0].body)

    def test_a_temporary_dkim_failure_keeps_the_domain(self):
        row = self.verify(self.claim())
        self.ses.verified, self.ses.dkim = True, "TEMPORARY_FAILURE"
        with dns_for(row.domain):
            row = domains.check(row)
        self.assertEqual(row.status, SenderDomain.Status.VERIFIED)
        self.ses.verified = False
        with dns_for(row.domain):
            row = domains.check(row)
        self.assertEqual(row.status, SenderDomain.Status.VERIFIED)
        self.ses.dkim = "FAILED"
        with dns_for(row.domain):
            row = domains.check(row)
        self.assertEqual(row.status, SenderDomain.Status.FAILED)

    def test_a_new_domain_still_needs_dkim_success(self):
        row = self.claim()
        self.ses.verified, self.ses.dkim = True, "TEMPORARY_FAILURE"
        with dns_for(row.domain):
            row = domains.check(row)
        self.assertEqual(row.status, SenderDomain.Status.PENDING)

    def test_drafts_and_new_utskick_use_the_verified_domain(self):
        draft = self.utskick(status=Utskick.Status.DRAFT)
        sent = self.utskick(status=Utskick.Status.SENT)
        row = self.verify(self.claim())
        draft.refresh_from_db()
        sent.refresh_from_db()
        self.assertEqual(draft.sender_domain_id, row.pk)
        self.assertIsNone(sent.sender_domain_id)
        # Väljer kunden ADX-domänen efteråt står valet kvar.
        Utskick.objects.filter(pk=draft.pk).update(sender_domain=None)
        with dns_for(row.domain):
            domains.check(row)
        draft.refresh_from_db()
        self.assertIsNone(draft.sender_domain_id)
        client = self.client_for(self.anna)
        client.post(reverse("flamingo:app_utskick_new"), {"namn": "Vinterkampanj"})
        fresh = Utskick.objects.get(account=self.account, name="Vinterkampanj")
        self.assertEqual(fresh.sender_domain_id, row.pk)

    def test_the_mx_priority_has_its_own_row_on_the_page(self):
        self.claim()
        page = self.client_for(self.anna).get(reverse("flamingo:app_utskick_domain"))
        self.assertContains(page, "<dt>Prioritet</dt>")
        self.assertContains(page, 'data-kt-copy="feedback-smtp.eu-west-1.amazonses.com"')
        self.assertNotContains(page, "10 feedback-smtp")


# ---------------------------------------------------------------------------
# Sändningen
# ---------------------------------------------------------------------------


class SendingReviewTests(EmailFixture, TestCase):
    def test_a_failed_freeze_never_sends_the_live_draft(self):
        self.people(2)
        with mock.patch("apps.utskick.email.render.snapshot", side_effect=OSError("tillfälligt")):
            u = self.freeze(self.utskick())
        self.assertEqual((u.status, u.pause_reason), (Utskick.Status.PAUSED, "content"))
        self.assertIsNotNone(u.frozen_at)
        self.assertFalse(email_loop.is_frozen(u))
        # Bekräftas det igen fryses mejlet om innan något skickas.
        result = state.confirm(
            u, actor=STAFF, nonce=state.issue_nonce(u), summary={"email": 2}, send_now=True
        )
        self.assertTrue(result.ok, result.error)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.SENDING)
        self.assertTrue(email_loop.is_frozen(u))
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(len(ses.calls), 2)

    def test_a_refreeze_that_fails_again_pauses_again(self):
        self.people(1)
        with mock.patch("apps.utskick.email.render.snapshot", side_effect=OSError("tillfälligt")):
            u = self.freeze(self.utskick())
            result = state.confirm(
                u, actor=STAFF, nonce=state.issue_nonce(u), summary={"email": 1}, send_now=True
            )
        self.assertFalse(result.ok)
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), (Utskick.Status.PAUSED, "content"))

    def test_a_refreeze_still_stops_at_a_host_waiting_for_adx(self):
        from .email.render import LinkSpot

        self.people(1)
        with mock.patch("apps.utskick.email.render.snapshot", side_effect=OSError("tillfälligt")):
            u = self.freeze(self.utskick())
        spots = [LinkSpot("b_Ab12Cd34Ef56", 0, "https://ny-vard.example/erbjudande", "Länk")]
        with mock.patch("apps.utskick.email.render.collect_links", return_value=spots):
            result = state.confirm(
                u, actor=STAFF, nonce=state.issue_nonce(u), summary={"email": 1}, send_now=True
            )
        self.assertFalse(result.ok)
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), (Utskick.Status.PAUSED, "content"))
        self.assertTrue(email_loop.is_frozen(u))
        self.assertEqual(result.error, links.PENDING_TEXT)

    def test_the_loop_never_builds_a_mail_without_the_snapshot(self):
        u = self.sending(2)
        Utskick.objects.filter(pk=u.pk).update(email_snapshot={})
        u.refresh_from_db()
        with self.assertRaises(email_loop.NotFrozen):
            email_loop.compose(u, u.recipients.first())
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(ses.calls, [])
        u.refresh_from_db()
        self.assertEqual((u.status, u.pause_reason), (Utskick.Status.PAUSED, "content"))
        self.assertEqual(u.stats["pause"]["note"], email_loop.NOT_FROZEN_TEXT)
        self.assertEqual(self.statuses(u), [RS.QUEUED, RS.QUEUED])

    def own_page(self):
        service = Service.objects.create(
            account=self.account, name="Badrumsrenovering", sales_mode=Service.SALES_QUOTE
        )
        campaign = Campaign.objects.create(
            account=self.account,
            service=service,
            name="Badrum Nacka",
            status=Campaign.STATUS_LIVE,
            page={"title": "Badrumsrenovering i Nacka", "questions": []},
        )
        return campaign, landing_page_url(campaign)

    def test_an_own_page_with_an_anchor_or_a_query_is_an_lp_link(self):
        from .email import blocks
        from .email.render import LinkSpot

        campaign, url = self.own_page()
        variants = [url + "#boka", url + "?utm_source=mejl", url.rstrip("/"), url]
        for variant in variants:
            self.assertEqual(blocks.own_page(self.account, variant), campaign, variant)
        u = self.utskick(status=Utskick.Status.DRAFT, email_doc={"blocks": [{"id": "x"}]})
        with mock.patch("apps.utskick.email.blocks.urls", return_value=variants):
            self.assertEqual(email_loop.email_link_problems(u), [])
        spots = [LinkSpot("b_Ab12Cd34Ef56", i, v, f"Länk {i}") for i, v in enumerate(variants)]
        with mock.patch("apps.utskick.email.render.collect_links", return_value=spots):
            u = self.sending(1)
        rows = TrackedLink.objects.filter(utskick=u).order_by("position")
        self.assertEqual({row.kind for row in rows}, {TrackedLink.Kind.LP})
        self.assertEqual({row.campaign_id for row in rows}, {campaign.pk})
        # Ankaret följer med till sidan, med ut och utm som vanligt.
        anchored = rows[0]
        self.assertEqual(links.bare_destination(anchored), url + "#boka")
        self.assertTrue(links.build_destination(anchored).endswith("#boka"))
        self.assertEqual(links.bare_destination(rows[1]), url)

    def test_one_unclear_answer_stops_the_loop_with_one_alert(self):
        u = self.sending(3)
        with transport.FakeSes(fail="InternalFailure", status=500) as ses:
            counts = self.run_email()
        self.assertEqual(counts.get("unknown"), 1)
        self.assertEqual(len(ses.calls), 1)
        self.assertEqual(self.statuses(u), [RS.QUEUED, RS.QUEUED, RS.UNKNOWN])
        subjects = [m.subject for m in mail.outbox]
        self.assertEqual(subjects.count("Utskick: SES svarar inte säkert"), 1)
        with transport.FakeSes(fail="InternalFailure", status=500) as ses:
            self.run_email()
        self.assertEqual(len(ses.calls), 1)
        subjects = [m.subject for m in mail.outbox]
        self.assertEqual(subjects.count("Utskick: SES svarar inte säkert"), 1)

    def test_mails_that_failed_after_ses_took_them_still_count(self):
        u = self.sending(2)
        with transport.FakeSes():
            self.run_email()
        self.assertEqual(email_loop.adx_month_count(self.account), 2)
        # En tillfällig studs eller 24 timmar som oklar: failed med sent_at.
        u.recipients.update(status=RS.FAILED)
        self.assertEqual(email_loop.adx_month_count(self.account), 2)
        self.assertEqual(health.day_count(self.account), 2)
        self.assertEqual(health.probe_count(u), 2)
        # Ett mejl SES aldrig tog emot (slingans _fail) räknas inte.
        u.recipients.update(sent_at=None)
        self.assertEqual(email_loop.adx_month_count(self.account), 0)
        self.assertEqual(health.day_count(self.account), 0)

    def test_an_ssl_error_may_have_reached_ses(self):
        from botocore.exceptions import (
            ConnectTimeoutError,
            EndpointConnectionError,
            ProxyConnectionError,
            SSLError,
        )

        endpoint = "https://email.eu-west-1.amazonaws.com"
        for error in (
            SSLError(endpoint_url=endpoint, error="EOF"),
            ProxyConnectionError(proxy_url="http://proxy.example"),
        ):
            with self.subTest(error=type(error).__name__):
                with transport.FakeSes(script=[error]):
                    sent = transport.send(outgoing(), kind=transport.PROBE)
                self.assertTrue(sent.unknown)
                self.assertFalse(sent.retry)
        for error in (
            EndpointConnectionError(endpoint_url=endpoint),
            ConnectTimeoutError(endpoint_url=endpoint),
        ):
            with self.subTest(error=type(error).__name__):
                with transport.FakeSes(script=[error]):
                    sent = transport.send(outgoing(), kind=transport.PROBE)
                self.assertEqual((sent.error, sent.retry, sent.unknown), ("connect", True, False))


class ProbeResumeTests(EmailFixture, TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(health, "PROBE_SIZE", 2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def failed_probe(self):
        u = self.sending(4)
        with transport.FakeSes():
            self.run_email()
        for recipient in u.recipients.filter(status=RS.SENT):
            events.apply(bounce(recipient))
        past = timezone.now() - timedelta(minutes=1)
        Utskick.objects.filter(pk=u.pk).update(hold_until=past)
        u.recipients.filter(status=RS.QUEUED).update(not_before=past)
        with transport.FakeSes():
            self.run_email()
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED_HEALTH)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.BOUNCES)
        return u

    def test_only_staff_gets_past_a_failed_probe(self):
        u = self.failed_probe()
        self.assertTrue(health.probe_failed(u))
        result = state.resume(u, actor=ANNA)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, state.STAFF_RESUMES_TEXT)
        client = self.client_for(self.anna)
        report = client.get(reverse("flamingo:app_utskick", args=[u.pk]))
        self.assertNotContains(report, "Ta bort studsade och fortsätt")
        self.assertContains(report, "ADX går igenom utskicket innan det kan fortsätta.")
        client.post(reverse("flamingo:app_utskick_state", args=[u.pk]), {"action": "fortsatt"})
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED_HEALTH)
        self.assertIsNone(UtskickSettings.objects.get(account=self.account).email_probe_passed_at)
        # Byrån har gått igenom listan och fortsätter: provet är godkänt.
        result = state.resume(u, actor=STAFF)
        self.assertTrue(result.ok, result.error)
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(len(ses.calls), 2)
        self.assertIsNotNone(
            UtskickSettings.objects.get(account=self.account).email_probe_passed_at
        )

    def test_a_customer_resume_never_passes_the_probe(self):
        u = self.failed_probe()
        # Även om kundens Fortsätt skulle ha gått igenom (äldre kod): provet består.
        stats = dict(u.stats or {})
        stats["resumed"] = {"staff": False, "at": timezone.now().isoformat()}
        Utskick.objects.filter(pk=u.pk).update(stats=stats, status=Utskick.Status.SENDING)
        u.refresh_from_db()
        self.assertEqual(health.probe_state(u), "failed")
        self.assertIsNone(UtskickSettings.objects.get(account=self.account).email_probe_passed_at)

    def test_bounces_outside_the_probe_are_still_the_customers(self):
        UtskickSettings.objects.filter(account=self.account).update(
            email_probe_passed_at=timezone.now()
        )
        u = self.sending(1)
        state.pause(u, Utskick.PauseReason.BOUNCES, now=timezone.now())
        u.refresh_from_db()
        self.assertFalse(health.probe_failed(u))
        report = self.client_for(self.anna).get(reverse("flamingo:app_utskick", args=[u.pk]))
        self.assertContains(report, "Ta bort studsade och fortsätt")


# ---------------------------------------------------------------------------
# Ticken
# ---------------------------------------------------------------------------


class TickShareTests(UtskickFixture, TestCase):
    def sms_time(self, competes):
        seen = {}

        def fake_send(now, phase_end, only=None):
            seen["left"] = phase_end - time.monotonic()
            return {}

        due = mock.Mock()
        due.exists.return_value = True
        with (
            mock.patch.object(tick, "work_exists", return_value=True),
            mock.patch.object(tick, "sms_due", return_value=due),
            mock.patch.object(tick, "_email_competes", return_value=competes),
            mock.patch.object(tick, "_email_due", return_value=False),
            mock.patch("apps.utskick.sending.sms.send_due", side_effect=fake_send),
        ):
            tick.run(budget=60)
        return seen["left"]

    def test_sms_shares_the_tick_with_waiting_mail(self):
        self.assertGreater(self.sms_time(False), 40)
        shared = self.sms_time(True)
        self.assertGreater(shared, 15)
        self.assertLess(shared, 25)

    def test_only_real_mail_while_email_is_on_competes(self):
        self.assertFalse(tick._email_competes(timezone.now()))


# ---------------------------------------------------------------------------
# Guiden och texterna
# ---------------------------------------------------------------------------


class GuideReviewTests(EmailFixture, TestCase):
    def step(self, utskick, name):
        return reverse("flamingo:app_utskick_step", args=[utskick.pk, name])

    def test_a_customer_without_sms_starts_with_email(self):
        client = self.client_for(self.anna)
        client.post(reverse("flamingo:app_utskick_new"), {"namn": "Höstbrev"})
        fresh = Utskick.objects.get(account=self.account, name="Höstbrev")
        self.assertEqual(fresh.channel_mode, Utskick.ChannelMode.EMAIL_ONLY)

    def test_a_locked_mode_is_never_kept_silently(self):
        u = self.utskick(status=Utskick.Status.DRAFT, channel_mode=Utskick.ChannelMode.SMS_ONLY)
        client = self.client_for(self.anna)
        page = client.get(self.step(u, "kanal"))
        html = page.content.decode()
        self.assertRegex(html, r'value="email_only" checked')
        self.assertNotRegex(html, r'value="sms_only" checked')
        response = client.post(self.step(u, "kanal"), {"syfte": "reklam"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, utskick_views.SMS_DISABLED_TEXT)
        u.refresh_from_db()
        self.assertEqual(u.channel_mode, Utskick.ChannelMode.SMS_ONLY)

    def test_email_only_hides_the_sms_sender(self):
        u = self.utskick(status=Utskick.Status.DRAFT)
        page = self.client_for(self.anna).get(self.step(u, "kanal"))
        self.assertContains(page, 'data-ut-show-when="kanal!=email_only" hidden')
        self.assertContains(page, "Avregistrerade får aldrig utskick.")

    def test_the_time_step_speaks_of_mail_for_email_only(self):
        u = self.utskick(status=Utskick.Status.DRAFT)
        page = self.client_for(self.anna).get(self.step(u, "tid"))
        self.assertContains(page, "När ska utskicket gå i väg?")
        self.assertNotContains(page, "sms:en")
        self.assertNotContains(page, "tidsfönstret")
        self.assertContains(page, "4 reklammejl per kontakt och vecka")
        both = self.utskick(status=Utskick.Status.DRAFT, channel_mode=Utskick.ChannelMode.BOTH)
        page = self.client_for(self.anna).get(self.step(both, "tid"))
        self.assertContains(page, "tidsfönstret")
        self.assertContains(page, "reklam-sms och 4 reklammejl per kontakt och vecka")

    def test_granska_links_each_email_row_to_where_it_is_fixed(self):
        u = self.utskick(status=Utskick.Status.DRAFT)
        items = [
            email_checks.Item(email_checks.BLOCKS, "adress", "address"),
            email_checks.Item(email_checks.BLOCKS, "värd", "hosts"),
            email_checks.Item(email_checks.BLOCKS, "avsändare", "sender"),
            email_checks.Item(email_checks.WARNS, "alt", "alt"),
            email_checks.Item(email_checks.WARNS, "omdömen", "reviews"),
            email_checks.Item(email_checks.BLOCKS, "ämne", "subject"),
            email_checks.Item(email_checks.WARNS, "saknas", "missing:efternamn"),
        ]
        with mock.patch("apps.utskick.email.checks.email_checks", return_value=items):
            rows = utskick_views._email_review_items(self.account, u, 3, timezone.now())
        by_text = {row["text"]: row for row in rows}
        self.assertEqual(by_text["adress"]["fix_url"], reverse("flamingo:app_business"))
        self.assertEqual(by_text["omdömen"]["fix_url"], reverse("flamingo:app_business"))
        self.assertEqual(by_text["avsändare"]["fix_url"], reverse("flamingo:app_utskick_domain"))
        self.assertEqual(by_text["alt"]["fix_url"], reverse("flamingo:app_media"))
        for text in ("adress", "värd", "avsändare", "alt", "omdömen"):
            self.assertNotIn("brev", by_text[text], text)
        self.assertNotIn("fix_url", by_text["värd"])
        self.assertTrue(by_text["ämne"]["brev"])
        self.assertTrue(by_text["saknas"]["brev"])

    def test_granska_checks_the_mail_links_too(self):
        u = self.utskick(status=Utskick.Status.DRAFT, email_doc={"blocks": [{"id": "x"}]})
        urls = ["https://exempelror.example/boka", "tel:+46701740601"]
        client = self.client_for(self.anna)
        with (
            mock.patch("apps.utskick.email.blocks.urls", return_value=urls),
            mock.patch(
                "apps.utskick.links.check_destinations",
                return_value={"https://exempelror.example/boka": True},
            ) as checked,
        ):
            self.assertEqual(utskick_views._email_link_urls(u), urls[:1])
            response = client.post(
                reverse("flamingo:app_utskick_link_check", args=[u.pk]),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(checked.call_args.args[1], urls[:1])
        self.assertEqual(response.json()["text"], "1 av 1 länkar svarar.")

    def test_a_missing_value_never_suggests_three_dots(self):
        text = email_checks.MISSING_EMPTY_BOX_TEXT.format(n="3", name="efternamn")
        self.assertNotIn("...", text)
        self.assertIn("Om ett värde saknas", text)

    def test_a_near_white_accent_gets_a_visible_button(self):
        faint = style.brev_styles(style.palette_for_accent("#FAFAFA"))
        self.assertIn("border:1px solid", faint["btn_td"])
        plain = style.brev_styles(style.palette_for_accent("#0E7C66"))
        self.assertNotIn("border:", plain["btn_td"])
        yellow = style.palette_for_accent("#FFD400")
        self.assertTrue(yellow.light)
        self.assertFalse(yellow.faint)
