"""
Avregistreringen i mejlet och webbversionen på klick.adx.se (README E.1,
E.5, F.4, H.2, H.6; J S3 test_s3_one_click).

    OneClickTests      List-Unsubscribe=One-Click: POST utan CSRF avregistrerar (200,
                       tom kropp), GET gör det aldrig, fungerar när mottagarraden och
                       kontakten är borta, när utskick är av för kontot, och två gånger
    PageTests          sidan med knappen: maskerad adress, nonce, ingen kaka, ingen
                       CSRF-tagg, kundens text efteråt, fel token och fel värd
    WebViewTests       "Visa i webbläsaren": mejlet från renderaren med CSP och
                       X-Frame-Options, testmejl, främmande mottagare och gamla länkar

Länkvärden testas med LINK_SETTINGS och HTTP_HOST="klick.adx.se". POST:arna
går med Client(enforce_csrf_checks=True), HTTP_ORIGIN="null" och utan
Referer, och inget svar får sätta en kaka. email.render.web_view
(Brev-byggarens) ersätts med en attrapp i WebViewTests.
"""

import re
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import contacts, keys, link_actions, links, tokens
from .models import (
    CHANNEL_EMAIL,
    Consent,
    ConsentLog,
    Recipient,
    Suppression,
    UtskickSettings,
)
from .test_s3_foundation import LINK_SETTINGS, make_utskick
from .testing import UtskickFixture, make_contact

KLICK = {"HTTP_HOST": "klick.adx.se", "HTTP_ORIGIN": "null"}
ONE_CLICK = "List-Unsubscribe=One-Click"
FORM = "application/x-www-form-urlencoded"
FN_RE = re.compile(r'name="fn" value="([^"]+)"')
ANNA_EMAIL = "anna@kund.example"


def path_of(url):
    return url.replace(links.email_link_base(), "")


class UnsubscribeFixture(UtskickFixture):
    def setUp(self):
        super().setUp()
        override = override_settings(**LINK_SETTINGS)
        override.enable()
        self.addCleanup(override.disable)
        self.client = Client(enforce_csrf_checks=True)
        self.contact = make_contact(
            self.account, first_name="Anna", last_name="Lind", email=ANNA_EMAIL
        )
        self.value_hash = keys.value_hash(CHANNEL_EMAIL, ANNA_EMAIL)
        self.url = path_of(links.unsubscribe_url(self.account.pk, self.value_hash))

    def one_click(self, url=None):
        return self.client.post(url or self.url, data=ONE_CLICK, content_type=FORM, **KLICK)

    def suppression(self):
        return Suppression.objects.filter(
            account=self.account, channel=CHANNEL_EMAIL, value_hash=self.value_hash
        ).first()

    def assert_no_cookie(self, response):
        self.assertEqual(response.cookies, {})
        self.assertNotIn("Set-Cookie", response.headers)


class OneClickTests(UnsubscribeFixture, TestCase):
    def test_the_url_in_the_header_is_this_view(self):
        self.assertTrue(self.url.startswith("/a/"))
        self.assertEqual(
            links.unsubscribe_url(self.account.pk, self.value_hash),
            f"https://klick.adx.se{self.url}",
        )

    def test_a_post_without_csrf_unsubscribes(self):
        response = self.one_click()
        self.assertEqual((response.status_code, response.content), (200, b""))
        self.assert_no_cookie(response)
        self.assertIn("no-store", response["Cache-Control"])
        suppression = self.suppression()
        self.assertEqual(suppression.reason, Suppression.Reason.LIST_UNSUB)
        consent = self.contact.consents.get(channel=CHANNEL_EMAIL)
        self.assertEqual(consent.status, Consent.Status.UNSUBSCRIBED)
        log = ConsentLog.objects.filter(contact=self.contact, channel=CHANNEL_EMAIL).first()
        self.assertEqual(
            (log.source, log.source_detail, log.by_label),
            ("list_unsub", "Avregistrering i e-postprogrammet", "Personen själv"),
        )
        self.assertTrue(log.ip_hash)

    def test_multipart_one_click_also_works(self):
        response = self.client.post(self.url, {"List-Unsubscribe": "One-Click"}, **KLICK)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.suppression())

    def test_a_get_never_unsubscribes(self):
        for method in ("get", "head"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(self.url, **KLICK)
                self.assertEqual(response.status_code, 200)
                self.assert_no_cookie(response)
        self.assertIsNone(self.suppression())
        self.assertEqual(
            self.contact.consents.get(channel=CHANNEL_EMAIL).status, Consent.Status.MISSING
        )

    def test_another_body_is_not_a_one_click(self):
        response = self.client.post(
            self.url, data="List-Unsubscribe=Nej", content_type=FORM, **KLICK
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.suppression())

    def test_it_works_after_the_recipient_row_and_the_contact_are_gone(self):
        utskick = make_utskick(self.account, status="sent")
        recipient = Recipient.objects.create(
            utskick=utskick, contact=self.contact, channel=CHANNEL_EMAIL, address=ANNA_EMAIL
        )
        contacts.delete_contact(self.contact, suppress=False)
        Recipient.objects.filter(pk=recipient.pk).delete()
        self.assertIsNone(self.suppression())
        response = self.one_click()
        self.assertEqual((response.status_code, response.content), (200, b""))
        self.assertEqual(self.suppression().reason, Suppression.Reason.LIST_UNSUB)
        log = ConsentLog.objects.get(value_hash=self.value_hash, source="list_unsub")
        self.assertIsNone(log.contact_id)

    def test_it_works_when_utskick_is_off_for_the_account(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        self.assertEqual(self.one_click().status_code, 200)
        self.assertIsNotNone(self.suppression())

    def test_twice_is_fine(self):
        self.one_click()
        response = self.one_click()
        self.assertEqual((response.status_code, response.content), (200, b""))
        self.assertEqual(Suppression.objects.filter(account=self.account).count(), 1)
        self.assertEqual(
            ConsentLog.objects.filter(contact=self.contact, source="list_unsub").count(), 1
        )

    def test_the_unsubscribe_is_tied_to_the_latest_mail_and_skips_the_queue(self):
        sent = make_utskick(self.account, status="sent")
        recipient = Recipient.objects.create(
            utskick=sent,
            contact=self.contact,
            channel=CHANNEL_EMAIL,
            address=ANNA_EMAIL,
            status=Recipient.Status.DELIVERED,
            sent_at=timezone.now() - timedelta(days=1),
        )
        later = make_utskick(self.account, status="sending")
        queued = Recipient.objects.create(
            utskick=later, contact=self.contact, channel=CHANNEL_EMAIL, address=ANNA_EMAIL
        )
        self.one_click()
        self.assertEqual(self.suppression().utskick_id, sent.pk)
        recipient.refresh_from_db()
        self.assertIsNotNone(recipient.stopped_at)
        queued.refresh_from_db()
        self.assertEqual((queued.status, queued.skip_reason), ("skipped", "suppressed"))

    def test_only_the_tokens_account_is_touched(self):
        other = make_contact(self.other_account, email=ANNA_EMAIL)
        self.one_click()
        self.assertFalse(Suppression.objects.filter(account=self.other_account).exists())
        self.assertEqual(other.consents.get(channel=CHANNEL_EMAIL).status, Consent.Status.MISSING)


class PageTests(UnsubscribeFixture, TestCase):
    def test_the_page_asks_with_a_button(self):
        UtskickSettings.objects.filter(account=self.account).update(
            privacy_url="https://exempelror.example/integritet/"
        )
        response = self.client.get(self.url, **KLICK)
        html = response.content.decode()
        self.assertIn("Vill du sluta få e-post från Exempelrör?", html)
        self.assertIn("a***@k***.example", html)
        self.assertNotIn(ANNA_EMAIL, html)
        self.assertNotIn("Anna", html, "aldrig ett namn")
        self.assertNotIn("csrfmiddlewaretoken", html)
        self.assertIn('value="avregistrera"', html)
        self.assertIn("Ändra vad du får", html)
        self.assertIn("/v/", html)
        self.assertIn("Så hanterar Exempelrör dina uppgifter", html)
        self.assertIn('href="https://exempelror.example/integritet/"', html)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn("no-store", response["Cache-Control"])
        self.assert_no_cookie(response)

    def test_the_button_needs_the_nonce(self):
        response = self.client.post(self.url, {"action": "avregistrera"}, **KLICK)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Sidan hann bli för gammal", status_code=400)
        self.assertIsNone(self.suppression())
        response = self.client.post(self.url, {"action": "avregistrera", "fn": "1.nej"}, **KLICK)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.suppression())

    def test_the_button_unsubscribes(self):
        fn = FN_RE.search(self.client.get(self.url, **KLICK).content.decode()).group(1)
        response = self.client.post(self.url, {"action": "avregistrera", "fn": fn}, **KLICK)
        self.assertEqual(response.status_code, 302)
        self.assert_no_cookie(response)
        self.assertEqual(self.suppression().reason, Suppression.Reason.LINK)
        log = ConsentLog.objects.filter(contact=self.contact, channel=CHANNEL_EMAIL).first()
        self.assertEqual((log.source, log.source_detail), ("link", "Avregistreringslänk i mejl"))
        UtskickSettings.objects.filter(account=self.account).update(
            unsubscribe_text="Hör av dig om du vill ha tillbaka utskicken."
        )
        html = self.client.get(self.url, **KLICK).content.decode()
        self.assertIn("Du får ingen mer e-post från Exempelrör.", html)
        self.assertIn("Hör av dig om du vill ha tillbaka utskicken.", html)
        self.assertNotIn('value="avregistrera"', html)

    def test_a_nonce_from_another_token_does_not_work(self):
        other = path_of(links.unsubscribe_url(self.account.pk, keys.value_hash("email", "b@x.se")))
        fn = FN_RE.search(self.client.get(other, **KLICK).content.decode()).group(1)
        response = self.client.post(self.url, {"action": "avregistrera", "fn": fn}, **KLICK)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.suppression())

    def test_without_a_contact_the_page_shows_no_address(self):
        url = path_of(links.unsubscribe_url(self.account.pk, keys.value_hash("email", "b@x.se")))
        html = self.client.get(url, **KLICK).content.decode()
        self.assertIn("Vill du sluta få e-post från Exempelrör?", html)
        self.assertNotIn("***", html)

    def test_a_bad_token_or_host_is_404(self):
        token = self.url.rsplit("/", 1)[1]
        forged = token[:-1] + ("A" if token[-1] != "A" else "B")
        for url, host in (
            (f"/a/{forged}", "klick.adx.se"),
            (path_of(links.unsubscribe_url(987654, self.value_hash)), "klick.adx.se"),
            (self.url, "k.adx.se"),
        ):
            with self.subTest(url=url, host=host):
                response = self.client.post(
                    url, data=ONE_CLICK, content_type=FORM, HTTP_HOST=host, HTTP_ORIGIN="null"
                )
                self.assertEqual(response.status_code, 404)
                self.assert_no_cookie(response)
        self.assertFalse(Suppression.objects.exists())
        self.assertEqual(self.client.get(self.url).status_code, 404, "inte på adx.se")

    def test_a_preference_token_is_not_an_unsubscribe_token(self):
        token = tokens.preference_token(self.account.pk, "email", self.value_hash)
        response = self.client.post(f"/a/{token}", data=ONE_CLICK, content_type=FORM, **KLICK)
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(self.suppression())

    def test_masked_email_helper(self):
        self.assertEqual(
            link_actions.masked_email(self.account, self.value_hash), "a***@k***.example"
        )
        self.assertEqual(link_actions.masked_email(self.other_account, self.value_hash), "")


# ---------------------------------------------------------------------------
# Visa i webbläsaren (/w/)
# ---------------------------------------------------------------------------

MAIL_HTML = "<!doctype html><html><body><table><tr><td>Hej Anna</td></tr></table></body></html>"


@override_settings(**LINK_SETTINGS)
class WebViewTests(UtskickFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.utskick = make_utskick(self.account, status="sent", subject="Höstservice")
        self.contact = make_contact(self.account, first_name="Anna", email=ANNA_EMAIL)
        self.recipient = Recipient.objects.create(
            utskick=self.utskick, contact=self.contact, channel=CHANNEL_EMAIL, address=ANNA_EMAIL
        )
        patcher = mock.patch("apps.utskick.email.render.web_view", return_value=MAIL_HTML)
        self.render = patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, utskick_id, recipient_id=None, host="klick.adx.se"):
        url = path_of(links.web_view_url(utskick_id, recipient_id))
        return self.client.get(url, HTTP_HOST=host)

    def test_the_mail_with_its_headers(self):
        response = self.get(self.utskick.pk, self.recipient.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), MAIL_HTML)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(
            response["Content-Security-Policy"],
            "default-src 'none'; img-src https:; style-src 'unsafe-inline'; "
            "base-uri 'none'; form-action 'none'",
        )
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(response.cookies, {})
        utskick, recipient = self.render.call_args.args
        self.assertEqual((utskick.pk, recipient.pk), (self.utskick.pk, self.recipient.pk))

    def test_a_test_mail_has_no_recipient(self):
        self.assertEqual(self.get(self.utskick.pk).status_code, 200)
        self.assertIsNone(self.render.call_args.args[1])

    def test_an_old_link_after_retention_shows_the_mail_without_personal_values(self):
        pk = self.recipient.pk
        self.recipient.delete()
        self.assertEqual(self.get(self.utskick.pk, pk).status_code, 200)
        self.assertIsNone(self.render.call_args.args[1])

    def test_a_recipient_of_another_utskick_is_404(self):
        other = make_utskick(self.other_account, status="sent")
        self.assertEqual(self.get(other.pk, self.recipient.pk).status_code, 404)
        self.render.assert_not_called()

    def test_bad_tokens_and_hosts(self):
        token = path_of(links.web_view_url(self.utskick.pk, self.recipient.pk)).rsplit("/", 1)[1]
        forged = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assertEqual(self.client.get(f"/w/{forged}", HTTP_HOST="klick.adx.se").status_code, 404)
        self.assertEqual(
            self.get(self.utskick.pk, self.recipient.pk, host="k.adx.se").status_code, 404
        )
        self.assertEqual(self.get(987654).status_code, 404)
        self.render.assert_not_called()

    def test_nothing_to_show_is_404(self):
        self.render.return_value = ""
        self.assertEqual(self.get(self.utskick.pk, self.recipient.pk).status_code, 404)

    def test_a_post_is_refused(self):
        url = path_of(links.web_view_url(self.utskick.pk, self.recipient.pk))
        response = self.client.post(url, HTTP_HOST="klick.adx.se", HTTP_ORIGIN="null")
        self.assertIn(response.status_code, (403, 405))
        self.assertEqual(response.cookies, {})
        self.render.assert_not_called()

    @override_settings(DEBUG=True)
    def test_local_images_over_http_are_allowed_in_development(self):
        response = self.get(self.utskick.pk, self.recipient.pk)
        self.assertIn("img-src https: http:", response["Content-Security-Policy"])

    def test_the_route_name(self):
        token = tokens.web_view_token(self.utskick.pk, self.recipient.pk)
        self.assertEqual(
            reverse("links:web_view", urlconf="config.urls_links", args=[token]), f"/w/{token}"
        )
