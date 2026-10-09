"""
Grunden för S2 (README J S2): modellerna och migreringsregeln (B.0 med
raderingsregeln i databasen), värdroutern för k.adx.se och klick.adx.se
(E.1, C.3), ASGI-spärren för MCP, adresserna till stubbarna (I.1),
inställningarna (C.4) och kopplingen till apps/sms (C.1, D.4).

    ModelTests              nya fält och regler på modellerna
    DbOnDeleteTests         varje ny främmande nyckel har sin regel i Postgres
    HostRouterTests         bara config.urls_links svarar på länkvärdarna
    AsgiLinkHostTests       MCP och OAuth finns inte på länkvärdarna
    UrlWiringTests          varje S2-adress finns, är kontots och 404 annars
    SmsBridgeTests          leveransrapporten flyttar mottagaren framåt
    SmsWrapperTests         den enda vägen till apps/sms, aldrig från demot
    CodeTests               sms-koderna
    SettingsTests           inställningarna och .env.example
"""

import asyncio
import re
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.models import ForeignKey
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import Lead
from apps.sms import hooks, service
from apps.sms.models import SmsAccount, SmsMessage

from . import dbfk, links, smsbridge
from .models import (
    Click,
    Contact,
    InboundMessage,
    LinkCode,
    Recipient,
    Thread,
    ThreadMessage,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from .testing import PHONE_ANNA, UtskickFixture

User = get_user_model()

LINK_SETTINGS = {
    "UTSKICK_LINK_HOSTS": ["k.adx.se", "klick.adx.se"],
    "UTSKICK_SMS_LINK_BASE": "https://k.adx.se",
    "UTSKICK_EMAIL_LINK_BASE": "https://klick.adx.se",
}

#: Migreringarna där S1 slutar: deras främmande nycklar känner S1-koden
#: till, så de behöver ingen regel i databasen (README B.0).
S1_NODES = [("utskick", "0001_kontakter"), ("flamingo", "0016_forfragans_kontakt")]


def make_utskick(account, **kwargs):
    data = {"name": "Höstservice värmepump", "sms_body": "Hej {förnamn|du}"}
    data.update(kwargs)
    return Utskick.objects.create(account=account, **data)


# ---------------------------------------------------------------------------
# Modellerna
# ---------------------------------------------------------------------------


class ModelTests(UtskickFixture, TestCase):
    def test_new_leads_get_activity_and_an_empty_attribution(self):
        lead = Lead.objects.create(account=self.account, name="Anna")
        lead.refresh_from_db()
        self.assertEqual(lead.attribution, {})
        self.assertLess(abs(lead.activity_at - timezone.now()), timedelta(minutes=1))

    def test_an_utskick_lead_never_goes_to_google(self):
        utskick = make_utskick(self.account)
        lead = Lead.objects.create(account=self.account, gclid="abc", utskick=utskick)
        self.assertFalse(lead.can_send_to_google)
        self.assertEqual(lead.queue_arrival_conversion(), None)
        lead.utskick = None
        self.assertTrue(lead.can_send_to_google)

    def test_a_reply_lead_is_named_as_a_reply(self):
        lead = Lead(account=self.account, source=Lead.SOURCE_REPLY)
        self.assertEqual(lead.display_name, "Svar på utskick")
        self.assertEqual(lead.get_source_display(), "Svar på utskick")
        self.assertEqual(lead.arrival_kind, "")

    def test_no_personal_data_in_str(self):
        utskick = make_utskick(self.account)
        contact = Contact.objects.create(account=self.account, first_name="Anna", source="manual")
        rows = [
            Recipient(pk=1, utskick=utskick, contact=contact, channel="sms", address=PHONE_ANNA),
            InboundMessage(pk=2, channel="sms", from_address=PHONE_ANNA, body="Hej Anna"),
            ThreadMessage(pk=3, direction="in", body="Hej Anna"),
            LinkCode(pk=4, code="Ab12Cd", kind="link"),
            Thread(pk=5, address=PHONE_ANNA, channel="sms"),
        ]
        for row in rows:
            with self.subTest(row=type(row).__name__):
                self.assertNotIn("Anna", str(row))
                self.assertNotIn("74060", str(row))
                self.assertNotIn("Ab12Cd", str(row))

    def test_one_recipient_per_contact_and_channel(self):
        utskick = make_utskick(self.account)
        contact = Contact.objects.create(account=self.account, source="manual")
        Recipient.objects.create(utskick=utskick, contact=contact, channel="sms", address="x")
        Recipient.objects.create(utskick=utskick, contact=contact, channel="email", address="y")
        # Utan kontakt (borttagen enligt GDPR) får flera rader finnas.
        Recipient.objects.create(utskick=utskick, contact=None, channel="sms")
        Recipient.objects.create(utskick=utskick, contact=None, channel="sms")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Recipient.objects.create(utskick=utskick, contact=contact, channel="sms")

    def test_codes_are_case_sensitive_and_unique(self):
        LinkCode.objects.create(code="Ab12Cd", kind="person", account=self.account, value_hash="h")
        LinkCode.objects.create(code="ab12cd", kind="person", account=self.account, value_hash="h")
        with self.assertRaises(IntegrityError), transaction.atomic():
            LinkCode.objects.create(
                code="Ab12Cd", kind="person", account=self.account, value_hash="h"
            )

    def test_one_link_key_per_utskick(self):
        utskick = make_utskick(self.account)
        TrackedLink.objects.create(
            account=self.account, utskick=utskick, kind="external", key="boka", destination="x"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrackedLink.objects.create(
                account=self.account, utskick=utskick, kind="external", key="boka", destination="y"
            )

    def test_listed_is_every_utskick_until_s5(self):
        make_utskick(self.account)
        self.assertEqual(Utskick.objects.listed().count(), 1)

    def test_the_reply_number_index_uses_the_setting(self):
        index = next(i for i in SmsMessage._meta.indexes if i.name == "sms_msg_reply_number")
        self.assertEqual(dict(index.condition.children), {"sender": settings.UTSKICK_REPLY_NUMBER})


# ---------------------------------------------------------------------------
# B.0: raderingsregeln i databasen
# ---------------------------------------------------------------------------


def _label(model_ref):
    text = str(model_ref)
    return text.split(".", 1)[0].lower() if "." in text else ""


def s1_foreign_keys():
    """(app, modell, fält) för främmande nycklar som rör utskick och fanns
    efter S1:s migreringar."""
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    state = loader.project_state(S1_NODES)
    found = set()
    for (app_label, model_name), model_state in state.models.items():
        for name, field in model_state.fields.items():
            if not isinstance(field, ForeignKey):
                continue
            target = _label(field.remote_field.model)
            if "utskick" in (app_label, target):
                found.add((app_label, model_name, name))
    return found


def foreign_keys_after_s1():
    """Främmande nycklar från S2 och framåt som rör apps/utskick: på en
    utskickstabell, eller pekande på en."""
    old = s1_foreign_keys()
    rows = []
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            if not isinstance(field, ForeignKey):
                continue
            meta = model._meta
            if "utskick" not in (meta.app_label, field.related_model._meta.app_label):
                continue
            if (meta.app_label, meta.model_name, field.name) in old:
                continue
            rows.append((model, field))
    return rows


class DbOnDeleteTests(UtskickFixture, TestCase):
    def test_every_new_foreign_key_has_its_rule_in_the_database(self):
        rows = foreign_keys_after_s1()
        # Alla S2:s (0002 och flamingo.0017), så att vakten inte är tom.
        self.assertGreaterEqual(len(rows), 37)
        for model, field in rows:
            action = dbfk.sql_action(field)
            if action is None:
                continue
            with self.subTest(field=f"{model._meta.label}.{field.name}"):
                rules = dbfk.rules(connection, model._meta.db_table)
                self.assertEqual(rules.get(field.column), dbfk.CONFDELTYPE[action])

    def test_the_s1_keys_are_left_as_they_were(self):
        old = s1_foreign_keys()
        self.assertIn(("utskick", "consent", "contact"), old)
        self.assertIn(("flamingo", "lead", "contact"), old)
        self.assertNotIn(("utskick", "recipient", "contact"), old)

    def raw_delete(self, table, pk):
        """Som en äldre version: utan Djangos kaskad, och med villkoren
        prövade direkt (annars först vid commit, som testet aldrig når)."""
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", [pk])
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_an_older_release_can_delete_a_contact(self):
        contact = Contact.objects.create(account=self.account, source="manual")
        utskick = make_utskick(self.account)
        recipient = Recipient.objects.create(utskick=utskick, contact=contact, channel="sms")
        thread = Thread.objects.create(account=self.account, contact=contact, channel="sms")
        click = Click.objects.create(account=self.account, contact=contact, channel="sms")
        code = LinkCode.objects.create(
            code="Xy12Ab", kind="confirm", account=self.account, value_hash="h", contact=contact
        )
        inbound = InboundMessage.objects.create(
            channel="sms", provider_id="i1", received_at=timezone.now(), contact=contact
        )
        self.raw_delete("utskick_contact", contact.pk)
        for row in (recipient, thread, click, code, inbound):
            row.refresh_from_db()
            self.assertIsNone(row.contact_id, type(row).__name__)

    def test_an_older_release_can_delete_a_lead_with_a_reply_thread(self):
        lead = Lead.objects.create(account=self.account, source=Lead.SOURCE_REPLY)
        thread = Thread.objects.create(account=self.account, channel="sms", lead=lead)
        ThreadMessage.objects.create(thread=thread, direction="in", body="Hej")
        self.raw_delete("flamingo_lead", lead.pk)
        self.assertFalse(Thread.objects.filter(pk=thread.pk).exists())
        self.assertFalse(ThreadMessage.objects.filter(thread_id=thread.pk).exists())

    def test_an_older_release_can_delete_a_user(self):
        user = User.objects.create_user("tillfallig")
        utskick = make_utskick(self.account, created_by=user, confirmed_by=user)
        self.raw_delete("auth_user", user.pk)
        utskick.refresh_from_db()
        self.assertEqual((utskick.created_by_id, utskick.confirmed_by_id), (None, None))

    def test_deleting_an_utskick_sets_the_s1_links_to_null(self):
        utskick = make_utskick(self.account)
        lead = Lead.objects.create(account=self.account, utskick=utskick)
        self.raw_delete("utskick_utskick", utskick.pk)
        lead.refresh_from_db()
        self.assertIsNone(lead.utskick_id)


# ---------------------------------------------------------------------------
# Värdroutern (E.1)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class HostRouterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)

    def get(self, host, path, client=None):
        return (client or Client()).get(path, HTTP_HOST=host)

    def assertNoCookies(self, response):
        self.assertEqual(response.cookies, {}, response.cookies)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_which_host_is_which(self):
        self.assertEqual(links.link_host_kind("k.adx.se"), "k")
        self.assertEqual(links.link_host_kind("K.ADX.SE:443"), "k")
        self.assertEqual(links.link_host_kind("klick.adx.se"), "klick")
        self.assertEqual(links.link_host_kind("adx.se"), "")
        self.assertEqual(links.link_host_kind("kk.adx.se"), "")
        self.assertEqual(links.link_host_kind(""), "")
        with override_settings(UTSKICK_LINK_HOSTS="k.localhost, klick.localhost"):
            self.assertEqual(links.link_host_kind("klick.localhost:8770"), "klick")

    def test_robots_and_home_on_both_hosts(self):
        for host in ("k.adx.se", "klick.adx.se"):
            with self.subTest(host=host):
                robots = self.get(host, "/robots.txt")
                self.assertEqual(robots.status_code, 200)
                self.assertEqual(robots.content.decode(), "User-agent: *\nDisallow: /\n")
                self.assertEqual(robots["X-Robots-Tag"], "noindex, nofollow")
                self.assertNoCookies(robots)
                home = self.get(host, "/")
                self.assertContains(home, "länkar i sms och mejl som skickas med ADX Flamingo")
                self.assertEqual(home["X-Robots-Tag"], "noindex, nofollow")
                self.assertNoCookies(home)

    def test_nothing_else_answers_on_a_link_host(self):
        staff = Client()
        staff.force_login(self.staff)
        paths = (
            "/manage/",
            "/manage/utskick/",
            "/flamingo/",
            "/flamingo/app/",
            "/flamingo/app/utskick/",
            "/kund/",
            "/admin/",
            "/utskick/exempelror/",
            "/api/sms/v1/usage/",
            "/api/utskick/46elks/inkommande/abc/",
            "/lp/en-sida/",
            "/sitemap.xml",
            "/Ab12Cd/",
        )
        for client in (Client(), staff):
            for path in paths:
                with self.subTest(path=path, staff=client is staff):
                    response = self.get("k.adx.se", path, client)
                    self.assertEqual(response.status_code, 404)
                    self.assertContains(response, "Länken har gått ut", status_code=404)
                    self.assertNoCookies(response)

    def test_adx_se_is_unchanged(self):
        robots = Client().get("/robots.txt")
        self.assertIn("Sitemap:", robots.content.decode())
        self.assertNotIn("X-Robots-Tag", robots.headers)

    def test_link_posts_need_no_csrf_cookie(self):
        client = Client(enforce_csrf_checks=True)
        for path in ("/s/Ab12Cd", "/p/Ab12Cd", "/b/Ab12Cd", "/Ab12Cd"):
            with self.subTest(path=path):
                response = client.post(path, {}, HTTP_HOST="k.adx.se", HTTP_ORIGIN="null")
                # Stubbarna svarar 404, aldrig CSRF:s 403.
                self.assertEqual(response.status_code, 404)
                self.assertNoCookies(response)

    def test_the_code_routes(self):
        self.assertEqual(
            reverse("links:click", urlconf="config.urls_links", args=["Ab12Cd"]), "/Ab12Cd"
        )
        self.assertEqual(
            reverse("links:sms_unsubscribe", urlconf="config.urls_links", args=["Ab12Cd"]),
            "/s/Ab12Cd",
        )

    def test_a_view_for_one_host_is_404_on_the_other(self):
        from django.http import Http404, HttpResponse

        view = links.on_link_host("k")(lambda request: HttpResponse("ok"))
        request = RequestFactory().get("/")
        request.link_host = "k"
        self.assertEqual(view(request).status_code, 200)
        for kind in ("klick", ""):
            request.link_host = kind
            with self.assertRaises(Http404):
                view(request)

    def test_private_and_sms_link(self):
        from django.http import HttpResponseRedirect

        response = links.private(HttpResponseRedirect("https://exempelror.example/"))
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(links.sms_link("Ab12Cd"), "k.adx.se/Ab12Cd")
        self.assertEqual(links.sms_link("Ab12Cd", "s"), "k.adx.se/s/Ab12Cd")

    def test_link_hosts_are_never_counted(self):
        from apps.analytics.models import PageView

        before = PageView.objects.count()
        self.get("k.adx.se", "/")
        self.assertEqual(PageView.objects.count(), before)


@override_settings(**LINK_SETTINGS)
class AsgiLinkHostTests(SimpleTestCase):
    """MCP och OAuth svarar aldrig på länkvärdarna (C.3); nginx nekar dem
    också (C.5)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.assistant.asgi_app import build_application

        # staticmethod: annars blir funktionen en metod på klassen.
        cls.app = staticmethod(build_application())

    def status(self, host, path):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", host.encode())],
            "scheme": "https",
            "server": ("adx.se", 443),
            "client": ("127.0.0.1", 1),
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "root_path": "",
        }
        asyncio.run(self.app(scope, receive, send))
        return sent[0]["status"]

    def test_mcp_and_oauth_are_404_on_link_hosts(self):
        for host in ("k.adx.se", "klick.adx.se:443"):
            for path in ("/mcp", "/mcp/", "/authorize", "/token", "/.well-known/oauth-x"):
                with self.subTest(host=host, path=path):
                    self.assertEqual(self.status(host, path), 404)


# ---------------------------------------------------------------------------
# Adresserna (I.1)
# ---------------------------------------------------------------------------


#: (url-namn, med pk, metod) för varje S2-adress i verktyget.
APP_ROUTES = [
    ("flamingo:app_utskick_list", False, "get"),
    ("flamingo:app_utskick_new", False, "post"),
    ("flamingo:app_utskick_settings", False, "get"),
    ("flamingo:app_utskick", True, "get"),
    ("flamingo:app_utskick_count", True, "get"),
    ("flamingo:app_utskick_sms_preview", True, "get"),
    ("flamingo:app_utskick_link_check", True, "post"),
    ("flamingo:app_utskick_test", True, "post"),
    ("flamingo:app_utskick_confirm", True, "post"),
    ("flamingo:app_utskick_state", True, "post"),
    ("flamingo:app_utskick_recipients", True, "get"),
    ("flamingo:app_utskick_save_list", True, "post"),
]
STEPS = ("mottagare", "kanal", "innehall", "tid", "granska")


class UrlWiringTests(UtskickFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.utskick = make_utskick(self.account)
        self.foreign = make_utskick(self.other_account)
        self.client = self.client_for(self.anna)

    def url(self, name, with_pk, utskick=None):
        return reverse(name, args=[(utskick or self.utskick).pk] if with_pk else [])

    def call(self, method, url):
        return getattr(self.client, method)(url)

    def test_every_route_answers_for_the_own_account(self):
        for name, with_pk, method in APP_ROUTES:
            with self.subTest(name=name):
                response = self.call(method, self.url(name, with_pk))
                # utskick-ui-byggaren (S2): vyerna är riktiga; en tom POST
                # skickar tillbaka (302) eller nekas (400), aldrig 404 eller 500.
                self.assertIn(response.status_code, (200, 302, 400, 501), response.status_code)
        for step in STEPS:
            url = reverse("flamingo:app_utskick_step", args=[self.utskick.pk, step])
            self.assertEqual(self.client.get(url).status_code, 200, step)
        self.assertEqual(
            self.client.get(f"/flamingo/app/utskick/{self.utskick.pk}/steg/okant/").status_code,
            404,
        )

    def test_another_accounts_utskick_is_404(self):
        for name, with_pk, method in APP_ROUTES:
            if not with_pk:
                continue
            with self.subTest(name=name):
                response = self.call(method, self.url(name, True, self.foreign))
                self.assertEqual(response.status_code, 404)
        url = reverse("flamingo:app_utskick_step", args=[self.foreign.pk, "granska"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_everything_is_404_when_utskick_is_off(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        for name, with_pk, method in APP_ROUTES:
            with self.subTest(name=name):
                self.assertEqual(self.call(method, self.url(name, with_pk)).status_code, 404)

    def test_post_routes_refuse_get(self):
        for name, with_pk, method in APP_ROUTES:
            if method == "post":
                with self.subTest(name=name):
                    self.assertEqual(self.client.get(self.url(name, with_pk)).status_code, 405)

    def test_the_menu_and_tabs(self):
        html = self.client.get(reverse("flamingo:app_utskick_list")).content.decode()
        self.assertIn(reverse("flamingo:app_utskick_list"), html)
        self.assertIn(reverse("flamingo:app_utskick_settings"), html)
        self.assertIn('<summary class="fl-subnav__summary">Utskick</summary>', html)
        settings_page = self.client.get(reverse("flamingo:app_utskick_settings")).content
        self.assertIn(
            '<summary class="fl-subnav__summary">Utskick: Inställningar</summary>',
            settings_page.decode(),
        )

    def test_the_lp_visit_beacon_answers_204(self):
        url = reverse("flamingo_public:visit_beacon", args=["en-sida"])
        self.assertEqual(url, "/lp/en-sida/besok/")
        response = Client(enforce_csrf_checks=True).post(url, {"ut": "x.y", "s": "30"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.cookies, {})

    def test_staff_in_view_as_reaches_the_pages(self):
        staff = self.client_for(self.staff)
        self.assertEqual(staff.get(reverse("flamingo:app_utskick_list")).status_code, 200)

    def test_inbox_reply_routes(self):
        own = Lead.objects.create(account=self.account, source=Lead.SOURCE_REPLY)
        other = Lead.objects.create(account=self.other_account, source=Lead.SOURCE_REPLY)
        # Inkorg-byggaren (S2): vyerna är riktiga och kräver en svarstråd.
        Thread.objects.create(account=self.account, channel="sms", lead=own, address=PHONE_ANNA)
        for name in ("flamingo:app_lead_reply", "flamingo:app_lead_unsubscribe"):
            with self.subTest(name=name):
                status = self.client.post(reverse(name, args=[own.pk])).status_code
                self.assertIn(status, (200, 302))
                self.assertEqual(self.client.post(reverse(name, args=[other.pk])).status_code, 404)
                self.assertEqual(self.client.get(reverse(name, args=[own.pk])).status_code, 405)
        url = reverse("flamingo:app_lead_reply", args=[own.pk])
        self.assertEqual(url, f"/flamingo/app/inkorg/{own.pk}/svara/")

    def test_the_46elks_address_is_404_with_an_empty_body_until_built(self):
        url = reverse("utskick_api:elks_inbound", args=["a" * 32])
        self.assertEqual(url, "/api/utskick/46elks/inkommande/" + "a" * 32 + "/")
        response = Client(enforce_csrf_checks=True).post(url, {"id": "x", "message": "STOPP"})
        self.assertEqual((response.status_code, response.content), (404, b""))

    def test_manage_routes_are_staff_only(self):
        inbound = InboundMessage.objects.create(
            channel="sms", provider_id="p1", received_at=timezone.now()
        )
        from .models import AllowedHost

        host = AllowedHost.objects.create(account=self.account, host="exempelror.example")
        urls = [
            reverse("manage:utskick_inbound_route", args=[inbound.pk]),
            reverse("manage:utskick_host_decide", args=[host.pk]),
            reverse("manage:utskick_info_override", args=[self.utskick.pk]),
            reverse("manage:utskick_probe"),
        ]
        staff = Client()
        staff.force_login(self.staff)
        for url in urls:
            with self.subTest(url=url):
                self.assertNotEqual(self.client.post(url).status_code, 501)
                # Inkorg-byggaren (S2): koppla eller lägg åt sidan, sedan översikten.
                # Länk-byggaren (S2): godkänn eller neka värden, sedan översikten.
                # Sändnings-byggaren (S2): undantag och provsms, sedan översikten.
                self.assertEqual(staff.post(url).status_code, 302)
                self.assertEqual(staff.get(url).status_code, 405)
        self.assertEqual(
            staff.post(reverse("manage:utskick_inbound_route", args=[999999])).status_code, 404
        )
        self.assertEqual(staff.get(reverse("manage:utskick_overview")).status_code, 200)


# ---------------------------------------------------------------------------
# Kopplingen till apps/sms (C.1, D.4)
# ---------------------------------------------------------------------------


@override_settings(
    SMS_SEND_LIVE=True, SMS_CALLBACK_BASE_URL="", SITE_BASE_URL="https://adx.example"
)
class SmsBridgeTests(UtskickFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.sms_account = SmsAccount.objects.create(
            customer=self.customer, is_enabled=True, sender_name="Exempelror"
        )
        self.utskick = make_utskick(self.account)
        self.message = SmsMessage.objects.create(
            account=self.sms_account,
            source="utskick",
            to=PHONE_ANNA,
            sender=settings.UTSKICK_REPLY_NUMBER,
            body="Hej",
            parts=1,
            status=SmsMessage.Status.SENT,
            provider_id="s1",
            sent_at=timezone.now(),
            reference=f"u{self.utskick.pk}:1",
        )
        self.recipient = Recipient.objects.create(
            utskick=self.utskick,
            channel="sms",
            address=PHONE_ANNA,
            status=Recipient.Status.SENT,
            sms_message=self.message,
        )
        thread = Thread.objects.create(account=self.account, channel="sms")
        self.out = ThreadMessage.objects.create(
            thread=thread, direction="out", body="Hej", sms_message=self.message, status="sending"
        )

    def test_the_bridge_is_registered(self):
        self.assertIn(smsbridge.sync_from_message, hooks.STATUS_CALLBACKS)
        self.assertIn(smsbridge.labels_for, hooks.LABELERS)

    def test_a_delivery_report_moves_the_recipient_forward(self):
        status, _ = service.apply_delivery_report(self.message.pk, "s1", "delivered")
        self.assertEqual(status, 200)
        self.recipient.refresh_from_db()
        self.out.refresh_from_db()
        self.assertEqual(self.recipient.status, "delivered")
        self.assertIsNotNone(self.recipient.delivered_at)
        self.assertEqual(self.out.status, "sent")
        # Aldrig bakåt.
        self.message.status = "sent"
        smsbridge.sync_from_message(self.message)
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.status, "delivered")

    def test_a_failed_delivery(self):
        service.apply_delivery_report(self.message.pk, "s1", "failed")
        self.recipient.refresh_from_db()
        self.out.refresh_from_db()
        self.assertEqual((self.recipient.status, self.out.status), ("failed", "failed"))
        self.assertEqual(self.recipient.error, smsbridge.FAILED_TEXT)

    def test_an_unknown_recipient_follows_the_agency_check(self):
        SmsMessage.objects.filter(pk=self.message.pk).update(
            status=SmsMessage.Status.RESERVED, provider_id="", needs_check=True
        )
        Recipient.objects.filter(pk=self.recipient.pk).update(status="unknown", sent_at=None)
        self.assertTrue(service.resolve_check(SmsMessage.objects.get(pk=self.message.pk), True))
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.status, "sent")
        self.assertIsNotNone(self.recipient.sent_at)

    def test_queued_and_skipped_recipients_are_never_touched(self):
        Recipient.objects.filter(pk=self.recipient.pk).update(status="skipped")
        service.apply_delivery_report(self.message.pk, "s1", "delivered")
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.status, "skipped")

    def test_the_portal_names_the_utskick(self):
        self.assertEqual(
            hooks.labels([self.message]), {self.message.pk: "Utskick: Höstservice värmepump"}
        )
        html = self.client_for(self.anna).get("/kund/sms/").content.decode()
        self.assertIn("Utskick: Höstservice värmepump", html)


class _FakeElks:
    """Står i för apps.sms.elks._post (som apps/sms/tests.FakeElks)."""

    def __init__(self):
        self.calls = []

    def __call__(self, fields):
        self.calls.append(dict(fields))
        base = {"status": "created", "parts": 1, "from": fields["from"], "to": fields["to"]}
        if fields.get("dryrun") == "yes":
            return {**base, "estimated_cost": 5200}
        return {**base, "id": f"s{len(self.calls):032x}", "cost": 5200}

    @property
    def sends(self):
        return [c for c in self.calls if c.get("dryrun") != "yes"]


@override_settings(
    SMS_SEND_LIVE=True,
    ELKS_API_USERNAME="test",
    ELKS_API_PASSWORD="test-losen",
    SMS_PROVIDER="46elks",
    SMS_CALLBACK_BASE_URL="",
    SMS_RATE_PER_MINUTE=60,
    SMS_GLOBAL_PER_MINUTE=80,
    UTSKICK_SMS_ACCOUNT_PER_MINUTE=45,
    UTSKICK_SMS_GLOBAL_PER_MINUTE=60,
)
class SmsWrapperTests(UtskickFixture, TestCase):
    def setUp(self):
        super().setUp()
        from unittest import mock

        from .sending import sms_wrapper

        self.wrapper = sms_wrapper
        self.fake = _FakeElks()
        patcher = mock.patch("apps.sms.elks._post", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sms_account = SmsAccount.objects.create(
            customer=self.customer, is_enabled=True, sender_name="Exempelror"
        )

    def send(self, account=None, **kwargs):
        data = {
            "to": PHONE_ANNA,
            "body": "Hej från Exempelrör",
            "sender": settings.UTSKICK_REPLY_NUMBER,
            "source": "utskick",
            "reference": "u1:1",
        }
        data.update(kwargs)
        return self.wrapper.send(account or self.account, **data)

    def test_sends_from_the_reply_number_with_its_source(self):
        out = self.send()
        self.assertTrue(out.ok, out.detail)
        msg = out.message
        self.assertEqual((msg.source, msg.api_key_id), ("utskick", None))
        self.assertEqual(msg.sender, "+46766860046")
        self.assertEqual(self.fake.sends[0]["from"], "+46766860046")

    def test_the_demo_never_reaches_apps_sms(self):
        self.account.is_demo = True
        with self.assertRaises(self.wrapper.DemoRefused):
            self.send()
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(SmsMessage.objects.exists())

    def test_wrong_keys_refuse(self):
        from . import keys
        from .models import Switchboard

        Switchboard.get_solo()
        Switchboard.objects.update(hash_fingerprint="0" * 64, link_fingerprint="0" * 64)
        keys.forget_verified()
        with self.assertRaises(keys.KeyMismatch):
            self.send()
        self.assertEqual(self.fake.calls, [])

    def test_without_an_sms_account(self):
        self.sms_account.delete()
        out = self.send()
        self.assertEqual(out.error, "sms_not_enabled")

    def test_headroom_only_for_mass_sends(self):
        self.assertEqual(self.wrapper.headroom_for("utskick"), (15, 20))
        self.assertEqual(self.wrapper.headroom_for("flow"), (15, 20))
        for source in ("reply", "system", "test"):
            self.assertEqual(self.wrapper.headroom_for(source), (0, 0))

    def test_utskick_stops_at_its_share_of_the_minute(self):
        for i in range(45):
            SmsMessage.objects.create(
                account=self.sms_account, to=PHONE_ANNA, body="x", status="sent", source="utskick"
            )
        self.assertEqual(self.send(reference="u1:2").error, "rate_limited")
        # Ett svar från Inkorgen har ingen marginal och går fram.
        self.assertTrue(self.send(source="reply", reference="t1").ok)


class CodeTests(UtskickFixture, TestCase):
    def test_codes_are_six_gsm7_characters(self):
        from apps.sms import encoding

        from . import codes

        batch = codes.new_codes(500)
        self.assertEqual(len(set(batch)), 500)
        for code in batch:
            self.assertRegex(code, r"^[A-Za-z0-9]{6}$")
        self.assertEqual(encoding.analyse(" ".join(batch[:20])).encoding, "gsm7")

    def test_confirm_codes_expire_after_a_day_and_survive_a_collision(self):
        from unittest import mock

        from . import codes

        LinkCode.objects.create(code="Taken1", kind="person", account=self.account, value_hash="h")
        with mock.patch.object(codes, "new_code", side_effect=["Taken1", "Fresh2"]):
            row = codes.create_confirm(self.account, value_hash="h", purpose="start")
        self.assertEqual((row.code, row.kind, row.purpose), ("Fresh2", "confirm", "start"))
        self.assertAlmostEqual(
            (row.expires_at - row.created_at).total_seconds(), 24 * 3600, delta=1
        )
        self.assertEqual(codes.find("Fresh2", "confirm"), row)
        self.assertIsNone(codes.find("Fresh2", "person"))
        self.assertIsNone(codes.find("fresh2", "confirm"))
        with mock.patch.object(codes, "new_code", return_value="Taken1"):
            with self.assertRaises(codes.CodeCollision):
                codes.create_confirm(self.account, value_hash="h", purpose="start")


# ---------------------------------------------------------------------------
# Inställningarna (C.4)
# ---------------------------------------------------------------------------


class SettingsTests(SimpleTestCase):
    def test_s2_defaults(self):
        self.assertEqual(settings.UTSKICK_REPLY_NUMBER, "+46766860046")
        self.assertEqual(settings.UTSKICK_SMS_ACCOUNT_PER_MINUTE, 45)
        self.assertEqual(settings.UTSKICK_SMS_GLOBAL_PER_MINUTE, 60)
        self.assertTrue(settings.UTSKICK_LINK_HOSTS)
        self.assertIn("apps.utskick.links.LinkHostMiddleware", settings.MIDDLEWARE)
        self.assertEqual(
            settings.MIDDLEWARE.index("apps.utskick.links.LinkHostMiddleware"),
            settings.MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
        )

    def test_every_utskick_setting_is_in_env_example(self):
        base = Path(settings.BASE_DIR) / "config" / "settings" / "base.py"
        example = (Path(settings.BASE_DIR) / ".env.example").read_text(encoding="utf-8")
        names = set(re.findall(r"^(UTSKICK_[A-Z_]+) = ", base.read_text("utf-8"), flags=re.M))
        self.assertIn("UTSKICK_ELKS_INBOUND_TOKEN", names)
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertIn(name, example)
