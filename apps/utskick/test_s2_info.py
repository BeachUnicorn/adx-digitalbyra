"""Informationsutskick (README H.5, D.9, J S2 test_s2_info): de kräver inget
samtycke och följer inte veckotaket, så de är inhägnade.

    InfoReviewTests     Granska blockerar: skälet, reklamord och priser,
                        Flamingo-sidor och länkar till andra webbplatser;
                        bekräftelsen nekas medan något blockerar
    InfoOverrideTests   byråns undantag med skäl (loggas, aldrig kunden),
                        släpper bara reklamorden, aldrig länkarna
    InfoSendTests       inget samtycke krävs men spärrlistan gäller,
                        veckotaket gäller inte och räknas inte, och raden om
                        STOPP eller /s/ står i varje sms
    InfoAlertTests      byrån larmas över 200 mottagare och vid fler än två
                        på 30 dagar; reklam larmar aldrig så

Inget når nätet: apps.sms.elks._post är FakeElks (test_s2_tick.EngineElks).
"""

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from . import composer
from . import consent as consents
from .models import CHANNEL_SMS, INFORMATION, REKLAM, Recipient, TrackedLink, Utskick
from .sending import checks, freeze
from .test_s2_tick import LIVE, NOW, EngineFixture
from .test_s2_ui import UiFixture

INFO_BODY = "Hej {förnamn|du}, Exempelrör har stängt mellan jul och nyår."
AD_BODY = "Hej {förnamn|du}, Exempelrör har 20 % rabatt på service i veckan."


def _flamingo_campaign(account):
    from apps.flamingo.models import Campaign, Service

    service = Service.objects.create(account=account, name="Rörjour")
    return Campaign.objects.create(
        account=account, service=service, name="Rörjour Nacka", page={"title": "Rörjour"}
    )


# ---------------------------------------------------------------------------
# Granska (I.6, H.5)
# ---------------------------------------------------------------------------


@LIVE
class InfoReviewTests(UiFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.person()
        self.person()

    def items(self, utskick):
        response = self.client.get(self.step(utskick, "granska"))
        self.assertEqual(response.status_code, 200)
        return response.context["review"]

    def blocking(self, utskick):
        return [i["text"] for i in self.items(utskick)["items"] if i["level"] == "block"]

    def info(self, body=INFO_BODY, **kwargs):
        kwargs.setdefault("info_reason", Utskick.InfoReason.OPPETTIDER)
        return self.utskick(
            body=body, purpose=INFORMATION, send_mode=Utskick.SendMode.NOW, **kwargs
        )

    def test_the_reason_is_required_and_named_in_granska(self):
        missing = self.info(info_reason="")
        self.assertIn(checks.INFO_REASON_TEXT, self.blocking(missing))
        other = self.info(info_reason=Utskick.InfoReason.ANNAT)
        self.assertIn(checks.INFO_OTHER_TEXT, self.blocking(other))
        good = self.info()
        review = self.items(good)
        self.assertFalse(review["blocking"], review["items"])
        self.assertIn(
            "Information: ändrade öppettider. Skickas utan samtycke, men aldrig till "
            "avregistrerade.",
            [i["text"] for i in review["items"]],
        )

    def test_offer_words_prices_and_codes_block(self):
        for body in (
            AD_BODY,
            "Hej, Exempelrör ger dig ett erbjudande i veckan.",
            "Hej, service hos Exempelrör kostar 995 kr i oktober.",
            "Hej, ange koden VINTER hos Exempelrör.",
            "Hej, det är gratis att boka hos Exempelrör.",
        ):
            with self.subTest(body=body):
                utskick = self.info(body=body)
                self.assertIn(checks.LOOKS_LIKE_AD_TEXT, self.blocking(utskick))
        # Platshållarnas namn räknas inte ({länk:erbjudande} är bara en länk).
        self.assertFalse(checks.looks_like_ad(composer.TOKEN_RE.sub(" ", "Läs {länk:kod}")))

    def test_a_flamingo_page_is_never_allowed(self):
        utskick = self.info(body=INFO_BODY + " {länk:sida}")
        TrackedLink.objects.create(
            account=self.account,
            utskick=utskick,
            kind=TrackedLink.Kind.LP,
            key="sida",
            campaign=_flamingo_campaign(self.account),
            destination="https://adx.se/lp/rorjour/",
        )
        self.assertIn(checks.INFO_LP_TEXT, self.blocking(utskick))

    def test_only_the_customers_own_site_may_be_linked(self):
        own = self.info(body=INFO_BODY + " {länk:boka}")
        self.assertFalse(self.items(own)["blocking"])
        other = self.info(body=INFO_BODY + " {länk:annan}", link=False)
        TrackedLink.objects.create(
            account=self.account,
            utskick=other,
            kind=TrackedLink.Kind.EXTERNAL,
            key="annan",
            destination="https://www.facebook.com/exempelror",
        )
        self.assertIn(checks.INFO_HOST_TEXT.format(host="www.facebook.com"), self.blocking(other))

    def test_a_blocked_information_utskick_cannot_be_confirmed(self):
        utskick = self.info(body=AD_BODY)
        nonce = self.granska_nonce(utskick)
        response = self.client.post(
            self.url("flamingo:app_utskick_confirm", utskick), {"nonce": nonce}
        )
        self.assertRedirects(response, self.step(utskick, "granska"), fetch_redirect_response=False)
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.DRAFT)
        self.assertIsNone(utskick.confirmed_at)

    def test_reklam_is_never_held_to_the_information_rules(self):
        utskick = self.utskick(body=AD_BODY, purpose=REKLAM, send_mode=Utskick.SendMode.NOW)
        self.assertEqual(checks.information_problems(utskick), [])
        self.assertNotIn(checks.LOOKS_LIKE_AD_TEXT, self.blocking(utskick))


# ---------------------------------------------------------------------------
# Byråns undantag (H.5)
# ---------------------------------------------------------------------------


@LIVE
class InfoOverrideTests(UiFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.person()
        self.utskick_row = self.utskick(
            body=AD_BODY,
            purpose=INFORMATION,
            info_reason=Utskick.InfoReason.ARENDE,
            send_mode=Utskick.SendMode.NOW,
        )
        self.override_url = reverse("manage:utskick_info_override", args=[self.utskick_row.pk])

    def staff_client(self):
        return self.client_for(self.staff)

    def test_the_customer_can_never_override(self):
        response = self.client.post(self.override_url, {"reason": "Jag vill"})
        self.assertNotEqual(response.status_code, 200)
        self.utskick_row.refresh_from_db()
        self.assertEqual(self.utskick_row.content_override, {})
        self.assertIn(checks.LOOKS_LIKE_AD_TEXT, checks.information_problems(self.utskick_row))

    def test_staff_overrides_with_a_reason_and_it_is_logged(self):
        client = self.staff_client()
        client.post(self.override_url, {"reason": " "})
        self.utskick_row.refresh_from_db()
        self.assertEqual(self.utskick_row.content_override, {})
        with self.assertLogs("apps.utskick.manage_sending", "WARNING") as logged:
            client.post(self.override_url, {"reason": "Lagstadgad prisinformation"})
        self.assertIn(f"användare {self.staff.pk}", logged.output[0])
        self.assertNotIn("Lagstadgad", logged.output[0])
        self.utskick_row.refresh_from_db()
        override = self.utskick_row.content_override
        self.assertEqual(override["reason"], "Lagstadgad prisinformation")
        self.assertEqual(override["user"], self.staff.pk)
        self.assertTrue(override["by"].startswith("ADX ("))
        self.assertTrue(override["at"])
        self.assertEqual(checks.information_problems(self.utskick_row), [])
        # Kunden mejlas aldrig om undantaget.
        self.assertEqual(mail.outbox, [])

    def test_the_override_never_releases_links(self):
        TrackedLink.objects.create(
            account=self.account,
            utskick=self.utskick_row,
            kind=TrackedLink.Kind.LP,
            key="sida",
            campaign=_flamingo_campaign(self.account),
            destination="https://adx.se/lp/rorjour/",
        )
        Utskick.objects.filter(pk=self.utskick_row.pk).update(sms_body=AD_BODY + " {länk:sida}")
        self.staff_client().post(self.override_url, {"reason": "Prisinformation"})
        self.utskick_row.refresh_from_db()
        self.assertEqual(checks.information_problems(self.utskick_row), [checks.INFO_LP_TEXT])

    def test_a_sent_or_reklam_utskick_takes_no_override(self):
        Utskick.objects.filter(pk=self.utskick_row.pk).update(status=Utskick.Status.SENT)
        self.staff_client().post(self.override_url, {"reason": "Sent"})
        reklam = self.utskick(body=AD_BODY, purpose=REKLAM)
        self.staff_client().post(
            reverse("manage:utskick_info_override", args=[reklam.pk]), {"reason": "Reklam"}
        )
        for row in (self.utskick_row, reklam):
            row.refresh_from_db()
            self.assertEqual(row.content_override, {})

    def test_the_agency_overview_lists_information_with_the_reason(self):
        page = self.staff_client().get(reverse("manage:utskick_overview"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Informationsutskick")
        self.assertContains(page, "Ärende")
        self.assertContains(page, self.override_url)


# ---------------------------------------------------------------------------
# Sändningen (D.3, D.4, H.5)
# ---------------------------------------------------------------------------


@LIVE
class InfoSendTests(EngineFixture, TestCase):
    def info(self, body=INFO_BODY, **kwargs):
        return self.utskick(
            body=body,
            purpose=INFORMATION,
            info_reason=Utskick.InfoReason.DRIFTSTORNING,
            **kwargs,
        )

    def sms_bodies(self):
        return [c["message"] for c in self.fake.sends]

    def test_no_consent_is_needed_but_a_suppression_always_wins(self):
        missing = self.person(status=consents.MISSING)
        declined = self.person(status=consents.DECLINED)
        stopped = self.person()
        consents.set_status(stopped, CHANNEL_SMS, consents.UNSUBSCRIBED, source="stop")
        u = self.freeze(self.info())
        rows = {r.contact_id: r for r in Recipient.objects.filter(utskick=u)}
        self.assertEqual(rows[missing.pk].status, Recipient.Status.QUEUED)
        self.assertEqual(rows[declined.pk].status, Recipient.Status.QUEUED)
        self.assertEqual(rows[stopped.pk].status, Recipient.Status.SKIPPED)
        self.assertEqual(rows[stopped.pk].skip_reason, Recipient.SkipReason.SUPPRESSED)

    def test_the_weekly_cap_neither_applies_nor_counts(self):
        kontakt = self.person()
        # Veckotaket (två reklam-sms per kontakt och vecka) är fyllt:
        # informationen går ändå.
        for _ in range(self.settings.weekly_cap_sms):
            old = self.utskick(status=Utskick.Status.SENT, body="Hej, Exempelrör.")
            Recipient.objects.create(
                utskick=old,
                contact=kontakt,
                channel=CHANNEL_SMS,
                address=kontakt.phone,
                status=Recipient.Status.DELIVERED,
                sent_at=NOW - timedelta(hours=1),
            )
        u = self.freeze(self.info())
        row = Recipient.objects.get(utskick=u, contact=kontakt)
        self.assertEqual(row.status, Recipient.Status.QUEUED)
        # Två informationsutskick samma vecka gör inte att reklam hoppas över.
        other = self.person()
        for _ in range(3):
            done = self.utskick(
                status=Utskick.Status.SENT,
                body=INFO_BODY,
                purpose=INFORMATION,
                info_reason=Utskick.InfoReason.BOKNING,
            )
            Recipient.objects.create(
                utskick=done,
                contact=other,
                channel=CHANNEL_SMS,
                address=other.phone,
                status=Recipient.Status.DELIVERED,
                sent_at=NOW - timedelta(hours=1),
            )
        reklam = self.freeze(self.utskick(body="Hej {förnamn|du}, Exempelrör har tid."))
        self.assertEqual(
            Recipient.objects.get(utskick=reklam, contact=other).status, Recipient.Status.QUEUED
        )

    def test_the_opt_out_line_is_in_every_information_sms(self):
        self.people(2)
        self.freeze(self.info())
        self.run_sms()
        self.assertEqual(len(self.fake.sends), 2)
        for body in self.sms_bodies():
            self.assertTrue(body.endswith(composer.STOP_LINE), body)
        self.assertTrue(all(c["from"] == "+46766860046" for c in self.fake.sends))

    def test_with_a_name_sender_the_line_is_the_unsubscribe_link(self):
        self.people(1)
        self.freeze(
            self.info(sms_sender_kind=Utskick.SenderKind.NAME, sms_sender_name="Exempelror")
        )
        self.run_sms()
        (body,) = self.sms_bodies()
        self.assertRegex(body, r"Avregistrera: k\.[a-z.]+(:\d+)?/s/[A-Za-z0-9]{6}$")
        self.assertNotIn("Svara STOPP", body)
        self.assertEqual(self.fake.sends[0]["from"], "Exempelror")

    def test_a_single_recipient_gets_the_line_too(self):
        self.people(1)
        self.freeze(self.info())
        self.run_sms()
        self.assertTrue(self.sms_bodies()[0].endswith(composer.STOP_LINE))


# ---------------------------------------------------------------------------
# Byråns larm (D.9, H.5)
# ---------------------------------------------------------------------------


@LIVE
class InfoAlertTests(EngineFixture, TestCase):
    SUBJECT = "Utskick: informationsutskick att granska"

    def info(self, **kwargs):
        return self.utskick(
            body=INFO_BODY,
            purpose=INFORMATION,
            info_reason=Utskick.InfoReason.OPPETTIDER,
            **kwargs,
        )

    def alerts(self):
        return [m for m in mail.outbox if m.subject == self.SUBJECT]

    def test_the_limits_are_the_contracts(self):
        self.assertEqual(freeze.INFO_BIG, 200)
        self.assertEqual(freeze.INFO_PER_30_DAYS, 2)

    def test_more_than_200_recipients_alert_the_agency(self):
        self.people(3)
        with mock.patch.object(freeze, "INFO_BIG", 3):
            self.freeze(self.info())
        self.assertEqual(self.alerts(), [])
        self.person()
        with mock.patch.object(freeze, "INFO_BIG", 3):
            self.freeze(self.info())
        (alert,) = self.alerts()
        self.assertIn("4 mottagare", alert.body)
        self.assertIn("Skäl: Ändrade öppettider.", alert.body)
        self.assertEqual(alert.to, ["byran@adx.example"])
        for message in mail.outbox:
            self.assertNotIn("+4670", message.body)

    def test_a_third_information_utskick_in_30_days_alerts(self):
        self.people(1)
        old = self.freeze(self.info())
        Utskick.objects.filter(pk=old.pk).update(frozen_at=NOW - timedelta(days=31))
        self.freeze(self.info())
        self.freeze(self.info())
        self.assertEqual(self.alerts(), [])
        self.freeze(self.info())
        (alert,) = self.alerts()
        self.assertIn("Kontot har 3 informationsutskick de senaste 30 dagarna.", alert.body)

    def test_reklam_never_alerts_as_information(self):
        self.people(3)
        with mock.patch.object(freeze, "INFO_BIG", 1):
            for _ in range(4):
                self.freeze(self.utskick())
        self.assertEqual(self.alerts(), [])

    def test_the_demo_never_alerts(self):
        from apps.flamingo.models import FlamingoAccount

        self.people(3)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        with mock.patch.object(freeze, "INFO_BIG", 1):
            self.freeze(self.info())
        self.assertEqual(self.alerts(), [])
