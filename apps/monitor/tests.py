"""
Övervakningen: rätt kontroller körs, avbrott och larm hanteras, kunden
ser bara det som är påslaget och bara sina egna domäner, statusendpointet
kräver nyckel. Nätverket är alltid mockat.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.projects.models import Customer

from . import checks
from .models import Check, Incident, Kind, MonitoredDomain, settings_for
from .runner import run_all, run_daily, run_quick

EMAIL = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "EMAIL_HOST_USER": "x",
    "EMAIL_HOST_PASSWORD": "y",
    "INQUIRY_NOTIFICATION_EMAIL": "staff@example.com",
}

UP = {"ok": True, "ms": 120, "status": 200, "final_url": "https://nordan.se/", "error": ""}
DOWN = {"ok": False, "ms": None, "status": 0, "error": "timeout"}


class Fixture(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("byra", password="x", is_staff=True)
        self.acme = Customer.objects.create(name="Acme AB")
        self.other = Customer.objects.create(name="Annan AB")
        self.contact = User.objects.create_user("anna@acme.se", email="anna@acme.se", password="x")
        self.acme.users.add(self.contact)
        self.domain = MonitoredDomain.objects.create(
            customer=self.acme, name="nordan.se", is_primary=True
        )
        self.foreign = MonitoredDomain.objects.create(customer=self.other, name="annan.se")
        self.monitor = settings_for(self.acme)

    def as_staff(self):
        c = Client()
        c.force_login(self.staff)
        return c

    def as_contact(self):
        c = Client()
        c.force_login(self.contact)
        return c


@override_settings(**EMAIL)
class RunnerTests(Fixture):
    def test_quick_records_uptime_and_skips_disabled_kinds(self):
        self.monitor.show_uptime = False
        self.monitor.show_response = False
        self.monitor.save()
        with mock.patch.object(checks, "check_uptime", return_value=UP) as up:
            run_quick(self.domain)
        up.assert_not_called()
        self.assertEqual(self.domain.checks.count(), 0)
        self.monitor.show_uptime = True
        self.monitor.save()
        with mock.patch.object(checks, "check_uptime", return_value=UP):
            run_quick(self.domain)
        check = self.domain.checks.get()
        self.assertEqual((check.kind, check.ok, check.ms), (Kind.UPTIME, True, 120))

    def test_incident_opens_on_second_failure_alerts_staff_once_and_closes(self):
        with mock.patch.object(checks, "check_uptime", return_value=DOWN):
            run_quick(self.domain)
        self.assertEqual(Incident.objects.count(), 0, "ett enstaka fel är inget avbrott")
        self.assertEqual(len(mail.outbox), 0)
        with mock.patch.object(checks, "check_uptime", return_value=DOWN):
            run_quick(self.domain)
            run_quick(self.domain)
        incident = Incident.objects.get()
        self.assertIsNone(incident.ended_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["staff@example.com"])
        self.assertIn("NERE: nordan.se", mail.outbox[0].subject)
        with mock.patch.object(checks, "check_uptime", return_value=UP):
            run_quick(self.domain)
        incident.refresh_from_db()
        self.assertIsNotNone(incident.ended_at)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("UPPE IGEN", mail.outbox[1].subject)
        for message in mail.outbox:
            self.assertNotIn("anna@acme.se", message.to)

    def test_daily_runs_enabled_checks_and_collects_attention(self):
        self.monitor.show_errors = True
        self.monitor.sentry_project = "nordan"
        self.monitor.save()
        with (
            mock.patch.object(
                checks, "check_ssl", return_value={"ok": True, "days_left": 10, "warning": True}
            ),
            mock.patch.object(
                checks,
                "check_domain",
                return_value={"ok": False, "days_left": 12, "registrar": "Loopia"},
            ),
            mock.patch.object(checks, "check_email", return_value={"ok": True, "rows": []}),
            mock.patch.object(
                checks, "check_security", return_value={"ok": True, "rows": [], "score": 5, "of": 6}
            ),
            mock.patch.object(
                checks, "check_performance", return_value={"ok": True, "strategies": {}}
            ) as perf,
            mock.patch.object(
                checks,
                "fetch_sentry_errors",
                return_value={"ok": True, "days": [], "series": [], "total_7d": 3, "total_30d": 9},
            ),
        ):
            attention = run_daily(self.domain, skip_slow=True)
        perf.assert_not_called()
        self.assertEqual(
            attention,
            [
                "certifikatet går ut om 10 dagar",
                "domänen går ut om 12 dagar (Loopia)",
                "3 fel i Sentry senaste 7 dagarna",
            ],
        )
        self.assertEqual(
            set(self.domain.checks.values_list("kind", flat=True)),
            {"ssl", "domain", "email", "security", "errors"},
        )

    def test_run_all_daily_mails_one_summary_to_staff(self):
        with (
            mock.patch.object(
                checks, "check_ssl", return_value={"ok": True, "days_left": 3, "warning": True}
            ),
            mock.patch.object(checks, "check_domain", return_value={"ok": True, "days_left": 300}),
            mock.patch.object(checks, "check_email", return_value={"ok": True, "rows": []}),
            mock.patch.object(checks, "check_security", return_value={"ok": True, "rows": []}),
            mock.patch.object(
                checks, "check_performance", return_value={"ok": True, "strategies": {}}
            ),
        ):
            count, attention = run_all(daily=True)
        self.assertEqual(count, 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["staff@example.com"])
        self.assertIn("nordan.se: certifikatet går ut om 3 dagar", mail.outbox[0].body)

    def test_snapshot_needs_key_and_url(self):
        self.monitor.show_server = True
        self.monitor.save()
        self.domain.status_url = "https://nordan.se/status/adx/"
        self.domain.save()
        with override_settings(ADX_STATUS_KEY=""):
            with mock.patch.object(checks, "check_uptime", return_value=UP):
                run_quick(self.domain)
        snap = self.domain.latest(Kind.SNAPSHOT)
        self.assertFalse(snap.ok)
        self.assertIn("ADX_STATUS_KEY", snap.data["error"])


@override_settings(ADX_STATUS_KEY="hemlig")
class StatusEndpointTests(TestCase):
    def test_requires_the_shared_key(self):
        self.assertEqual(Client().get("/status/adx/").status_code, 403)
        self.assertEqual(Client().get("/status/adx/", HTTP_X_ADX_KEY="fel").status_code, 403)
        r = Client().get("/status/adx/", HTTP_X_ADX_KEY="hemlig")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["db"], "ok")
        self.assertEqual(data["app"], "adx-platform")
        self.assertIn("server", data)
        self.assertIn("visits", data)
        self.assertIn("sessions_7d", data["visits"])

    @override_settings(ADX_STATUS_KEY="")
    def test_is_absent_without_a_key(self):
        self.assertEqual(Client().get("/status/adx/").status_code, 404)


class PortalTests(Fixture):
    def _seed(self):
        now = timezone.now()
        for i in range(10):
            Check.objects.create(
                domain=self.domain,
                kind=Kind.UPTIME,
                ok=i != 3,
                ms=100 + i,
                checked_at=now - timedelta(minutes=5 * i),
            )
        Check.objects.create(
            domain=self.domain,
            kind=Kind.SSL,
            ok=True,
            data={"days_left": 60, "not_after": "2026-11-19", "issuer": "Let's Encrypt"},
        )
        Check.objects.create(
            domain=self.domain,
            kind=Kind.DOMAIN,
            ok=True,
            data={
                "apex": "nordan.se",
                "registrar": "Loopia AB",
                "expires": "2027-03-01",
                "days_left": 160,
                "nameservers": ["ns1.loopia.se"],
                "locked": True,
            },
        )
        Check.objects.create(
            domain=self.foreign,
            kind=Kind.SSL,
            ok=True,
            data={"days_left": 5, "not_after": "2026-09-25", "issuer": "Hemlig CA"},
        )

    def test_status_page_shows_only_enabled_panels_and_own_domains(self):
        self._seed()
        html = self.as_contact().get("/kund/status/").content.decode()
        self.assertIn("nordan.se", html)
        self.assertNotIn("annan.se", html)
        self.assertNotIn("Hemlig CA", html)
        self.assertIn("Online", html)
        self.assertIn("90,0 %", html)
        self.assertIn("60 dagar kvar", html)
        self.assertIn("Loopia AB", html)
        self.assertNotIn("Server</div>", html, "serverpanelen är av som standard")
        self.assertNotIn("timer", html)
        self.monitor.show_ssl = False
        self.monitor.show_domain = False
        self.monitor.note = "Vi bytte till snabbare disk i helgen."
        self.monitor.save()
        html = self.as_contact().get("/kund/status/").content.decode()
        self.assertNotIn("60 dagar kvar", html)
        self.assertNotIn("Loopia AB", html)
        self.assertIn("snabbare disk", html)

    def test_report_page(self):
        self._seed()
        today = timezone.localdate()
        html = self.as_contact().get(f"/kund/rapport/{today.year}/{today.month}/").content.decode()
        self.assertIn("nordan.se", html)
        self.assertIn("90,0 %", html)
        self.assertIn("Vad vi gjorde", html)
        self.assertEqual(self.as_contact().get("/kund/rapport/2030/1/").status_code, 404)
        lst = self.as_contact().get("/kund/rapporter/").content.decode()
        self.assertIn(f"/kund/rapport/{today.year}/{today.month}/", lst)

    def test_no_domains_yet(self):
        self.domain.delete()
        html = self.as_contact().get("/kund/status/").content.decode()
        self.assertIn("inte uppsatt än", html)


class ManageTests(Fixture):
    def test_add_domain_toggle_and_run(self):
        client = self.as_staff()
        client.post(
            f"/manage/kunder/{self.acme.pk}/overvakning/doman/",
            {"name": "https://www.Acme.se/", "platform": "on"},
        )
        added = MonitoredDomain.objects.get(name="acme.se")
        self.assertEqual(added.status_url, "https://acme.se/status/adx/")
        self.assertFalse(added.is_primary, "nordan.se var redan primär")
        client.post(
            f"/manage/kunder/{self.acme.pk}/overvakning/",
            {"show_uptime": "on", "show_server": "on", "note": "Hej", "sentry_project": "acme"},
        )
        self.monitor.refresh_from_db()
        self.assertTrue(self.monitor.show_server)
        self.assertFalse(self.monitor.show_ssl)
        self.assertEqual(self.monitor.note, "Hej")
        with (
            mock.patch.object(checks, "check_uptime", return_value=UP),
            mock.patch.object(
                checks, "fetch_status_endpoint", return_value={"ok": True, "db": "ok", "ms": 50}
            ),
        ):
            r = client.post(f"/manage/kunder/{self.acme.pk}/overvakning/kor/", {"mode": "quick"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            Check.objects.filter(domain__customer=self.acme, kind=Kind.UPTIME).count(), 2
        )
        html = client.get(f"/manage/kunder/{self.acme.pk}/").content.decode()
        self.assertIn("acme.se", html)
        self.assertIn("Kör snabbkontroll nu", html)

    def test_drift_overview_lists_problems_first(self):
        Check.objects.create(
            domain=self.foreign, kind=Kind.UPTIME, ok=False, data={"error": "timeout"}
        )
        Check.objects.create(
            domain=self.foreign, kind=Kind.UPTIME, ok=False, data={"error": "timeout"}
        )
        Incident.objects.create(domain=self.foreign)
        Check.objects.create(domain=self.domain, kind=Kind.UPTIME, ok=True, ms=90)
        html = self.as_staff().get("/manage/drift/").content.decode()
        self.assertLess(html.index("annan.se"), html.index("nordan.se"))
        self.assertIn("pågående avbrott", html)

    def test_contacts_cannot_reach_manage_monitor(self):
        r = self.as_contact().post(
            f"/manage/kunder/{self.acme.pk}/overvakning/", {"show_uptime": "on"}
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/kund/", r["Location"])


class CheckParsingTests(TestCase):
    def test_security_rows_from_headers(self):
        with mock.patch.object(
            checks,
            "_get",
            return_value=(
                200,
                {"strict-transport-security": "max-age=1", "server": "nginx/1.2"},
                b"",
                10,
                "https://x.se/",
            ),
        ):
            result = checks.check_security("x.se")
        titles = {r["titel"]: r["status"] for r in result["rows"]}
        self.assertEqual(titles["HTTPS"], "ok")
        self.assertEqual(titles["HSTS"], "ok")
        self.assertEqual(titles["X-Content-Type-Options"], "varning")
        self.assertEqual(titles["Server-header"], "varning")

    def test_uptime_failure_is_a_result_not_an_exception(self):
        with mock.patch.object(checks, "_get", side_effect=OSError("nej")):
            result = checks.check_uptime("x.se")
        self.assertFalse(result["ok"])
        self.assertIn("nej", result["error"])

    def test_sentry_without_config_is_a_soft_error(self):
        with override_settings(SENTRY_ORG_SLUG="", SENTRY_API_TOKEN=""):
            self.assertFalse(checks.fetch_sentry_errors("x")["ok"])

    def test_se_domains_use_internetstiftelsens_whois(self):
        answer = "\n".join(
            [
                "domain:           adx.se",
                "holder:           (not shown)",
                "created:          2014-06-23",
                "expires:          2027-11-23",
                "nserver:          ns-877.awsdns-45.net",
                "nserver:          ns-81.awsdns-10.com",
                "status:           ok",
                "registrar:        Rymdweb AB",
            ]
        )
        with mock.patch.object(checks, "_whois", return_value=answer) as whois:
            result = checks.check_domain("www.adx.se")
        whois.assert_called_once_with("whois.iis.se", "adx.se")
        self.assertEqual(result["registrar"], "Rymdweb AB")
        self.assertEqual(result["expires"], "2027-11-23")
        self.assertEqual(result["nameservers"], ["ns-877.awsdns-45.net", "ns-81.awsdns-10.com"])
        self.assertTrue(result["ok"])
        with mock.patch.object(checks, "_whois", return_value="% no match\n"):
            self.assertFalse(checks.check_domain("finnsinte.se")["ok"])
