"""
Kundernas avsändardomäner (README B.3, J S3 "Domain flow", I.9, J S3
test_s3_domains): reglerna för anspråk (adx.se, gratis e-post, publika
suffix, förälder och barn till ett annat kontos domän, AlreadyExists),
CreateEmailIdentity med Easy DKIM och MAIL FROM studs.<domän>, posterna,
kontrollen med dnspython (mockad) och GetEmailIdentity, att en
verifiering avbryter andras anspråk, en domän per verifierad ägare,
utgången efter 14 dagar, regeln att identiteten bara tas bort när appen
skapade den, sidan Avsändare och svar med den egna svarsadressen, och
byråns sidor ("Släpp spärren", domänen, provmejlet, "Avsluta utskick").

Inget når nätet: aws.client("sesv2") är FakeSesApi, dns.resolver.resolve är
mockad och SES-sändningarna går till transport.FakeSes.
"""

from datetime import timedelta
from unittest import mock

import dns.exception
import dns.resolver
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import tokens
from .email import domains, transport
from .models import SenderDomain, Utskick, UtskickSettings
from .test_s3_transport import EmailFixture

STATUS = SenderDomain.Status
TOKENS = ["tok1abc", "tok2abc", "tok3abc"]


class FakeSesApi:
    """SES v2-API:t för identiteterna (api-klienten): sparar varje anrop."""

    def __init__(self, exists=(), verified=False, dkim="PENDING"):
        self.calls = []
        self.exists = set(exists)
        self.verified = verified
        self.dkim = dkim

    def _error(self, code, operation):
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": code, "Message": "fake"}}, operation)

    def create_email_identity(self, **kwargs):
        self.calls.append(("create", kwargs))
        if kwargs["EmailIdentity"] in self.exists:
            raise self._error("AlreadyExistsException", "CreateEmailIdentity")
        self.exists.add(kwargs["EmailIdentity"])
        return {
            "IdentityType": "DOMAIN",
            "VerifiedForSendingStatus": False,
            "DkimAttributes": {"SigningEnabled": True, "Status": "PENDING", "Tokens": TOKENS},
        }

    def put_email_identity_mail_from_attributes(self, **kwargs):
        self.calls.append(("mail_from", kwargs))
        return {}

    def get_email_identity(self, EmailIdentity):
        self.calls.append(("get", {"EmailIdentity": EmailIdentity}))
        return {
            "VerifiedForSendingStatus": self.verified,
            "DkimAttributes": {"Status": self.dkim, "Tokens": TOKENS, "SigningEnabled": True},
            "MailFromAttributes": {
                "MailFromDomain": f"studs.{EmailIdentity}",
                "MailFromDomainStatus": "SUCCESS" if self.verified else "PENDING",
                "BehaviorOnMxFailure": "USE_DEFAULT_VALUE",
            },
        }

    def delete_email_identity(self, EmailIdentity):
        self.calls.append(("delete", {"EmailIdentity": EmailIdentity}))
        self.exists.discard(EmailIdentity)
        return {}

    def names(self):
        return [name for name, _kwargs in self.calls]


class Rdata:
    def __init__(self, text):
        self.text = text

    def to_text(self):
        return self.text


def dns_for(domain, *, dkim=True, mx=True, spf=True, dmarc=True, timeout=False):
    """En attrapp för dns.resolver.resolve med posterna för domain."""
    answers = {}
    if dkim:
        for token in TOKENS:
            answers[(f"{token}._domainkey.{domain}", "CNAME")] = [f"{token}.dkim.amazonses.com."]
    if mx:
        answers[(f"studs.{domain}", "MX")] = ["10 feedback-smtp.eu-west-1.amazonses.com."]
    if spf:
        answers[(f"studs.{domain}", "TXT")] = ['"v=spf1 include:amazonses.com ~all"']
    if dmarc:
        answers[(f"_dmarc.{domain}", "TXT")] = ['"v=DMARC1; p=quarantine"']

    def resolve(name, rdtype, lifetime=None):
        if timeout:
            raise dns.exception.Timeout()
        if (name, rdtype) not in answers:
            raise dns.resolver.NXDOMAIN()
        return [Rdata(text) for text in answers[(name, rdtype)]]

    return mock.patch("dns.resolver.resolve", side_effect=resolve)


class DomainFixture(EmailFixture):
    def setUp(self):
        super().setUp()
        self.ses = FakeSesApi()
        patcher = mock.patch("apps.utskick.aws.client", side_effect=lambda *a, **k: self.ses)
        patcher.start()
        self.addCleanup(patcher.stop)

    def claim(self, domain="exempelror.example", account=None, **kwargs):
        kwargs.setdefault("from_local", "hej")
        kwargs.setdefault("from_name", "Exempelrör")
        return domains.claim(account or self.account, domain, user=self.anna, **kwargs)


# ---------------------------------------------------------------------------
# Namnet och reglerna
# ---------------------------------------------------------------------------


class RuleTests(DomainFixture, TestCase):
    def test_normalize(self):
        for raw, expected in (
            ("Exempelror.SE", "exempelror.se"),
            ("https://www.exempelror.se/kontakt?x=1", "exempelror.se"),
            ("hej@exempelror.se", "exempelror.se"),
            ("exempelrör.se.", "exempelrör.se".encode("idna").decode()),
            ("mejl.exempelror.se", "mejl.exempelror.se"),
            ("exempelror", ""),
            ("-dalig.se", ""),
            ("192.168.0.1", ""),
            ("", ""),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(domains.normalize(raw), expected)

    def test_refusals(self):
        for domain, text in (
            ("adx.se", domains.ADX_TEXT),
            ("utskick.adx.se", domains.ADX_TEXT),
            ("kund.adx.se", domains.ADX_TEXT),
            ("gmail.com", domains.FREEMAIL_TEXT),
            ("mail.telia.com", domains.FREEMAIL_TEXT),
            ("co.uk", domains.SUFFIX_TEXT),
            ("se", domains.INVALID_TEXT),
            ("github.io", domains.SUFFIX_TEXT),
            ("inte en domän", domains.INVALID_TEXT),
        ):
            with self.subTest(domain=domain):
                self.assertEqual(domains.claim_problem(self.account, domain), text)
        self.assertEqual(domains.claim_problem(self.account, "exempelror.co.uk"), "")

    def test_parent_and_child_of_another_accounts_domain(self):
        SenderDomain.objects.create(
            account=self.other_account, domain="annanfirma.example", from_name="Annan"
        )
        for domain in ("annanfirma.example", "mejl.annanfirma.example"):
            with self.subTest(domain=domain):
                self.assertEqual(domains.claim_problem(self.account, domain), domains.IN_USE_TEXT)
        SenderDomain.objects.create(
            account=self.other_account,
            domain="post.tredje.example",
            from_name="Annan",
            status=STATUS.VERIFIED,
        )
        self.assertEqual(domains.claim_problem(self.account, "tredje.example"), domains.IN_USE_TEXT)
        # En borttagen eller utgången domän gör inget anspråk.
        SenderDomain.objects.filter(domain="annanfirma.example").update(status=STATUS.EXPIRED)
        self.assertEqual(domains.claim_problem(self.account, "annanfirma.example"), "")

    def test_one_domain_at_a_time(self):
        self.claim()
        self.assertEqual(domains.claim_problem(self.account, "annan.example"), domains.OWN_TEXT)
        self.assertEqual(domains.claim_problem(self.account, "exempelror.example"), "")


# ---------------------------------------------------------------------------
# Anspråket hos SES
# ---------------------------------------------------------------------------


class ClaimTests(DomainFixture, TestCase):
    def test_create_identity_with_easy_dkim_and_mail_from(self):
        row = self.claim("Exempelror.example", from_local="Info", from_name=" Exempelrör  AB ")
        self.assertEqual(row.status, STATUS.PENDING)
        self.assertTrue(row.ses_created)
        self.assertEqual(row.dkim_tokens, TOKENS)
        self.assertEqual(row.from_address, "info@exempelror.example")
        self.assertEqual(row.from_name, "Exempelrör AB")
        self.assertEqual(row.created_by, self.anna)
        create, mail_from = self.ses.calls
        self.assertEqual(
            create[1],
            {
                "EmailIdentity": "exempelror.example",
                "DkimSigningAttributes": {"NextSigningKeyLength": "RSA_2048_BIT"},
            },
        )
        self.assertEqual(
            mail_from[1],
            {
                "EmailIdentity": "exempelror.example",
                "MailFromDomain": "studs.exempelror.example",
                "BehaviorOnMxFailure": "USE_DEFAULT_VALUE",
            },
        )

    def test_an_existing_identity_is_never_adopted(self):
        self.ses.exists.add("exempelror.example")
        with self.assertRaises(domains.DomainRefused) as caught:
            self.claim()
        self.assertEqual(caught.exception.text, domains.IN_USE_TEXT)
        self.assertFalse(SenderDomain.objects.exists())
        self.assertEqual(self.ses.names(), ["create"])
        self.assertIn(
            "Utskick: en kund vill skicka från en domän som redan används",
            [m.subject for m in mail.outbox],
        )
        self.assertNotIn("anna@exempelror.example", " ".join(m.to[0] for m in mail.outbox))

    def test_a_second_claim_alerts_the_agency(self):
        self.claim()
        with self.assertRaises(domains.DomainRefused):
            self.claim(account=self.other_account)
        self.assertEqual(SenderDomain.objects.count(), 1)
        self.assertTrue(mail.outbox)
        self.assertTrue(all(m.to == ["byran@adx.example"] for m in mail.outbox))

    def test_the_same_domain_again_only_updates_the_sender(self):
        first = self.claim()
        again = self.claim(from_local="info", from_name="Nytt namn")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(again.from_address, "info@exempelror.example")
        self.assertEqual(self.ses.names().count("create"), 1)

    def test_a_bad_local_part(self):
        with self.assertRaises(domains.DomainRefused) as caught:
            self.claim(from_local="två ord")
        self.assertEqual(caught.exception.text, domains.LOCAL_TEXT)

    def test_without_aws_nothing_is_kept(self):
        from botocore.exceptions import EndpointConnectionError

        def broken(*args, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://email.eu-west-1.amazonaws.com")

        self.ses.create_email_identity = broken
        with self.assertRaises(domains.DomainRefused) as caught:
            self.claim()
        self.assertEqual(caught.exception.text, domains.SES_TEXT)
        self.assertFalse(SenderDomain.objects.exists())


# ---------------------------------------------------------------------------
# Posterna och kontrollen
# ---------------------------------------------------------------------------


class CheckTests(DomainFixture, TestCase):
    def test_the_records(self):
        row = self.claim()
        records = domains.records(row)
        self.assertEqual(
            [(r.kind, r.short, r.value) for r in records],
            [
                ("CNAME", "tok1abc._domainkey", "tok1abc.dkim.amazonses.com"),
                ("CNAME", "tok2abc._domainkey", "tok2abc.dkim.amazonses.com"),
                ("CNAME", "tok3abc._domainkey", "tok3abc.dkim.amazonses.com"),
                ("MX", "studs", "feedback-smtp.eu-west-1.amazonses.com"),
                ("TXT", "studs", "v=spf1 include:amazonses.com ~all"),
                ("TXT", "_dmarc", "v=DMARC1; p=none"),
            ],
        )
        self.assertEqual(records[0].name, "tok1abc._domainkey.exempelror.example")
        # Prioriteten är ett eget fält hos de flesta DNS-tjänster (UX-granskningen).
        self.assertEqual([r.priority for r in records if r.kind == "MX"], [10])
        self.assertEqual({r.priority for r in records if r.kind != "MX"}, {None})

    def test_everything_in_place_verifies(self):
        row = self.claim()
        rival = SenderDomain.objects.create(
            account=self.other_account,
            domain="mejl.exempelror.example",
            from_name="Annan",
            ses_created=True,
        )
        self.ses.verified, self.ses.dkim = True, "SUCCESS"
        with dns_for("exempelror.example"):
            row = domains.check(row)
        self.assertEqual(row.status, STATUS.VERIFIED)
        self.assertIsNotNone(row.verified_at)
        self.assertTrue(domains.dns_ok(row))
        self.assertEqual(row.checks["dmarc"]["state"], "ok")
        self.assertTrue(row.ses_snapshot["VerifiedForSendingStatus"])
        rival.refresh_from_db()
        self.assertEqual(rival.status, STATUS.FAILED)
        self.assertIn(("delete", {"EmailIdentity": "mejl.exempelror.example"}), self.ses.calls)
        self.assertEqual(domains.sendable(self.account, row.pk), row)
        self.assertIsNone(domains.sendable(self.other_account, row.pk))
        self.assertEqual(domains.summary(self.account)["dns_text"], "SPF, DKIM och DMARC OK")

    def test_missing_records_stay_pending(self):
        row = self.claim()
        with dns_for("exempelror.example", dkim=False, dmarc=False):
            row = domains.check(row)
        self.assertEqual(row.status, STATUS.PENDING)
        self.assertEqual(row.checks["dkim1"]["state"], "missing")
        self.assertEqual(row.checks["mx"]["state"], "ok")
        self.assertEqual(row.checks["dmarc"]["state"], "missing")
        self.assertEqual(domains.summary(self.account)["dns_text"], "DKIM och DMARC saknas")

    def test_a_wrong_value_and_a_dns_timeout(self):
        row = self.claim()
        wrong = mock.patch(
            "dns.resolver.resolve",
            side_effect=lambda name, rdtype, lifetime=None: [Rdata("annan.example.")],
        )
        with wrong:
            row = domains.check(row)
        self.assertEqual(row.checks["dkim1"]["state"], "wrong")
        self.assertEqual(row.checks["dkim1"]["seen"], "annan.example")
        with dns_for("exempelror.example", timeout=True):
            row = domains.check(row)
        self.assertEqual(row.checks["spf"]["state"], "unknown")

    def test_a_verified_domain_that_stops_working(self):
        row = self.claim()
        self.ses.verified, self.ses.dkim = True, "SUCCESS"
        with dns_for("exempelror.example"):
            row = domains.check(row)
        self.ses.verified, self.ses.dkim = False, "FAILED"
        with dns_for("exempelror.example", dkim=False):
            row = domains.check(row)
        self.assertEqual(row.status, STATUS.FAILED)
        self.assertIn(
            "Utskick: en verifierad avsändardomän fungerar inte längre",
            [m.subject for m in mail.outbox],
        )
        # Rättat: verifierad igen.
        self.ses.verified, self.ses.dkim = True, "SUCCESS"
        with dns_for("exempelror.example"):
            row = domains.check(row)
        self.assertEqual(row.status, STATUS.VERIFIED)

    def test_only_one_verified_owner(self):
        first = self.claim()
        second = SenderDomain.objects.create(
            account=self.other_account, domain="exempelror.example", from_name="Annan"
        )
        self.ses.verified, self.ses.dkim = True, "SUCCESS"
        with dns_for("exempelror.example"):
            domains.check(first)
            second = domains.check(second)
        self.assertEqual(second.status, STATUS.FAILED)
        self.assertEqual(
            SenderDomain.objects.filter(domain="exempelror.example", status=STATUS.VERIFIED)
            .get()
            .pk,
            first.pk,
        )

    def test_fourteen_days_without_verification(self):
        row = self.claim()
        SenderDomain.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(days=15)
        )
        with dns_for("exempelror.example", dkim=False):
            counts = domains.check_due()
        self.assertEqual(counts.get("expired"), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, STATUS.EXPIRED)
        self.assertIn(("delete", {"EmailIdentity": "exempelror.example"}), self.ses.calls)
        self.assertIn("Utskick: en avsändardomän gick ut", [m.subject for m in mail.outbox])
        # Kontot kan försöka igen (identiteten finns inte längre hos SES).
        again = self.claim()
        self.assertEqual(again.status, STATUS.PENDING)

    def test_the_daily_check_covers_pending_and_verified(self):
        self.claim()
        SenderDomain.objects.create(
            account=self.other_account,
            domain="annanfirma.example",
            from_name="Annan",
            status=STATUS.VERIFIED,
            verified_at=timezone.now(),
        )
        SenderDomain.objects.create(
            account=self.other_account,
            domain="gammal.example",
            from_name="Annan",
            status=STATUS.REMOVED,
        )
        with dns_for("exempelror.example"):
            counts = domains.check_due()
        self.assertEqual(counts.get("checked"), 2)


# ---------------------------------------------------------------------------
# Ta bort
# ---------------------------------------------------------------------------


class RemoveTests(DomainFixture, TestCase):
    def test_the_identity_goes_only_when_we_created_it(self):
        ours = self.claim()
        domains.remove(ours, user=self.anna)
        self.assertIn(("delete", {"EmailIdentity": "exempelror.example"}), self.ses.calls)
        ours.refresh_from_db()
        self.assertEqual(ours.status, STATUS.REMOVED)
        theirs = SenderDomain.objects.create(
            account=self.account, domain="ny.example", from_name="X", ses_created=False
        )
        self.ses.calls.clear()
        domains.remove(theirs, user=self.anna)
        self.assertEqual(self.ses.calls, [])

    def test_a_domain_in_use_by_a_scheduled_utskick_stays(self):
        row = self.claim()
        draft = self.utskick(status=Utskick.Status.DRAFT, sender_domain=row)
        busy = self.utskick(status=Utskick.Status.SCHEDULED, sender_domain=row)
        with self.assertRaises(domains.DomainRefused):
            domains.remove(row, user=self.anna)
        Utskick.objects.filter(pk=busy.pk).update(status=Utskick.Status.SENT)
        domains.remove(row, user=self.anna)
        draft.refresh_from_db()
        self.assertIsNone(draft.sender_domain_id, "utkastet skickas från ADX-domänen")
        busy.refresh_from_db()
        self.assertEqual(busy.sender_domain_id, row.pk, "ett skickat utskick pekar kvar")


# ---------------------------------------------------------------------------
# Sidan Avsändare och svar (I.9)
# ---------------------------------------------------------------------------


class DomainPageTests(DomainFixture, TestCase):
    def url(self):
        return reverse("flamingo:app_utskick_domain")

    def post(self, client, **data):
        return client.post(self.url(), data)

    def test_the_page_for_the_customer_and_staff(self):
        for user in (self.anna, self.staff):
            with self.subTest(user=user.username):
                response = self.client_for(user).get(self.url())
                self.assertEqual(response.status_code, 200)
                self.assertIn("Använd din egen domän", response.content.decode())

    def test_claim_check_help_and_remove(self):
        client = self.client_for(self.anna)
        response = self.post(client, action="claim", domain="exempelror.example", from_local="hej")
        self.assertEqual(response.status_code, 302)
        row = SenderDomain.objects.get(account=self.account)
        html = client.get(self.url()).content.decode()
        self.assertIn("tok1abc._domainkey", html)
        self.assertIn('data-kt-copy="tok1abc.dkim.amazonses.com"', html)
        self.assertIn("Väntar på DNS", html)
        with dns_for("exempelror.example"):
            self.post(client, action="check")
            SenderDomain.objects.filter(pk=row.pk).update(checked_at=timezone.now())
            self.post(client, action="check")
        self.assertEqual(self.ses.names().count("get"), 1, "en gång i minuten")
        mail.outbox.clear()
        self.post(client, action="help")
        self.assertEqual([m.to for m in mail.outbox], [["byran@adx.example"]])
        # Larmet länkar byråns sida för domänen och bär inga namn (alerts.py).
        body = mail.outbox[0].body
        self.assertIn(reverse("manage:utskick_domain_admin", args=[row.pk]), body)
        self.assertNotIn("Anna", body)
        self.post(client, action="remove")
        row.refresh_from_db()
        self.assertEqual(row.status, STATUS.REMOVED)

    def test_a_refused_claim_shows_the_text(self):
        response = self.post(self.client_for(self.anna), action="claim", domain="gmail.com")
        self.assertEqual(response.status_code, 400)
        self.assertIn(domains.FREEMAIL_TEXT, response.content.decode())

    def test_an_own_reply_address_on_the_verified_domain(self):
        self.verified_domain()
        client = self.client_for(self.anna)
        self.post(
            client, action="reply_mode", reply_mode="own", own_reply_to="Hej@Exempelror.example"
        )
        row = UtskickSettings.objects.get(account=self.account)
        self.assertEqual(row.email_reply_mode, "own")
        self.assertEqual(row.own_reply_to, "hej@exempelror.example")
        self.assertNotIn("Skicka bekräftelselänken", client.get(self.url()).content.decode())

    def test_an_other_address_is_confirmed_with_a_link(self):
        client = self.client_for(self.anna)
        self.post(client, action="reply_mode", reply_mode="own", own_reply_to="anna@annan.example")
        html = client.get(self.url()).content.decode()
        self.assertIn("Skicka bekräftelselänken till anna@annan.example", html)
        with transport.FakeSes() as ses:
            self.post(client, action="reply_send")
        self.assertEqual(len(ses.calls), 1)
        message = ses.messages[0]
        self.assertEqual(str(message["To"]), "anna@annan.example")
        self.assertEqual(ses.tags(), {"k": "replyconf", "a": str(self.account.pk)})
        link = [w for w in message.get_content().split() if "/svarsadress/" in w][0]
        path = "/" + link.split("/", 3)[3]
        # GET bekräftar aldrig.
        self.assertEqual(client.get(path).status_code, 200)
        row = UtskickSettings.objects.get(account=self.account)
        self.assertIsNone(row.own_reply_to_confirmed_at)
        response = client.post(path)
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertIsNotNone(row.own_reply_to_confirmed_at)
        # Ny adress: bekräftelsen gäller inte längre.
        self.post(client, action="reply_mode", reply_mode="own", own_reply_to="ny@annan.example")
        row.refresh_from_db()
        self.assertIsNone(row.own_reply_to_confirmed_at)

    def test_staff_must_tick_the_box_to_mail_the_customer(self):
        UtskickSettings.objects.filter(account=self.account).update(
            email_reply_mode="own", own_reply_to="anna@annan.example"
        )
        staff = self.client_for(self.staff)
        with transport.FakeSes() as ses:
            self.post(staff, action="reply_send")
            self.assertEqual(ses.calls, [])
            self.post(staff, action="reply_send", som_adx="1")
        self.assertEqual(len(ses.calls), 1)

    def test_a_bad_or_foreign_confirm_token(self):
        UtskickSettings.objects.filter(account=self.account).update(
            email_reply_mode="own", own_reply_to="anna@annan.example"
        )
        client = self.client_for(self.anna)
        other = tokens.reply_confirm_token(self.other_account.pk, "anna@annan.example")
        url = reverse("flamingo:app_utskick_reply_confirm", args=[other])
        self.assertIn("Länken gäller inte längre", client.get(url).content.decode())
        client.post(url)
        row = UtskickSettings.objects.get(account=self.account)
        self.assertIsNone(row.own_reply_to_confirmed_at)


# ---------------------------------------------------------------------------
# Byråns sidor
# ---------------------------------------------------------------------------


class AgencyTests(DomainFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.staff_client = Client()
        self.staff_client.force_login(self.staff)

    def test_the_overview_shows_the_email_panel(self):
        self.claim()
        UtskickSettings.objects.filter(account=self.account).update(
            email_blocked_at=timezone.now(), email_blocked_reason="bounces"
        )
        html = self.staff_client.get(reverse("manage:utskick_overview")).content.decode()
        self.assertIn('id="epost"', html)
        self.assertIn("exempelror.example", html)
        self.assertIn("Släpp spärren", html)
        self.assertIn("Skicka provmejl", html)

    def test_release_the_block(self):
        UtskickSettings.objects.filter(account=self.account).update(
            email_blocked_at=timezone.now(), email_blocked_reason="bounces"
        )
        url = reverse("manage:utskick_health_release", args=[self.account.pk])
        self.assertEqual(self.client_for(self.anna).post(url).status_code, 302)
        self.assertIsNotNone(UtskickSettings.objects.get(account=self.account).email_blocked_at)
        self.staff_client.post(url, {"note": "Listan rensad med kunden"})
        row = UtskickSettings.objects.get(account=self.account)
        self.assertIsNone(row.email_blocked_at)
        self.assertEqual(row.email_released_by, self.staff)

    def test_the_domain_page(self):
        row = self.claim()
        url = reverse("manage:utskick_domain_admin", args=[row.pk])
        html = self.staff_client.get(url).content.decode()
        self.assertIn("<thead>", html)
        self.assertIn("tok1abc.dkim.amazonses.com", html)
        with dns_for("exempelror.example"):
            self.staff_client.post(url, {"action": "check"})
        row.refresh_from_db()
        self.assertIsNotNone(row.checked_at)

    def test_the_probe_mail(self):
        mail.outbox.clear()
        with transport.FakeSes() as ses:
            response = self.staff_client.post(
                reverse("manage:utskick_probe"),
                {"kind": "email", "to": "byra@adx.example", "customer": self.customer.pk},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(ses.calls), 1)
        self.assertEqual(ses.tags(), {"k": "probe", "a": str(self.account.pk)})
        message = ses.messages[0]
        self.assertIn("List-Unsubscribe-Post", message)
        self.assertTrue(str(message["Reply-To"]).endswith("@svar.utskick.adx.se"))
        self.assertEqual(mail.outbox, [], "ingen kund mejlas")

    def test_ending_utskick_removes_the_domains_and_their_identity(self):
        row = self.claim()
        self.utskick(status=Utskick.Status.SENT, sender_domain=row)
        from .manage_views import end_account

        with self.captureOnCommitCallbacks(execute=True):
            end_account(self.account, self.staff)
        self.assertFalse(SenderDomain.objects.filter(account=self.account).exists())
        self.assertIn(("delete", {"EmailIdentity": "exempelror.example"}), self.ses.calls)
