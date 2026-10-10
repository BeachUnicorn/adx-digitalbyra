"""
Länkar, de namngivna länkarna och QR-koderna (README E.1, E.3, E.8, I.11,
J S4 test_s4_links; S4, länk-byggaren).

    NamedLinkFormTests   Ny länk: slugen (form, ledig, gjord av beskrivningen),
                         E.8-reglerna, väntande och nekad värd, främmande kampanj,
                         taket, demot, byrån i kundvyn
    LinkPageTests        en länks sida: siffrorna, spara (aldrig slugen), ta bort,
                         404 för ett utskicks egen länk och ett annat kontos
    LinkListTests        listan: namngivna länkar med chips och läge, utskickens
                         personliga länkar per utskick, siffrorna
    QrTests              QR-koden för en namngiven länk och för anmälningssidan
    NamedClickTests      klick.adx.se/<public_slug>/<slug>: 302 med ut, klicket med
                         kanalen named, bottar, HEAD, 20 rader i timmen, väntande
                         värd, bara på klick, förfrågan via länken och uppräkningen
    AdxParameterTests    adx= bara till en skriptdomän vars skript har setts

Länkvärdarna testas med LINK_SETTINGS och HTTP_HOST="klick.adx.se", utan
kakor. Inget når nätet (byråns larm går till Djangos testlåda).
"""

from datetime import timedelta
from unittest import mock
from urllib.parse import urlsplit

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.exports import landing_page_url
from apps.flamingo.models import Campaign, FlamingoAccount, Lead, Service

from . import links, optin, qr, tokens
from .app_views import links as link_views
from .models import AllowedHost, Click, LinkCode, Recipient, SiteSnippet, TrackedLink, Utskick
from .test_s2_links import IPHONE, LinkFixture, query
from .test_s4_foundation import LINK_SETTINGS, make_named
from .testing import make_contact

KLICK = {"HTTP_HOST": "klick.adx.se"}
AGENCY = {"INQUIRY_NOTIFICATION_EMAIL": "byran@adx.example"}
NEW_URL = reverse("flamingo:app_link_new")


def form(**data):
    values = {"beskrivning": "Affisch i verkstaden", "mal": "extern", "adress": "", "slug": ""}
    values.update(data)
    return values


# ---------------------------------------------------------------------------
# Ny länk
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS, **AGENCY)
class NamedLinkFormTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)

    def post(self, client=None, follow=False, **data):
        return (client or self.client).post(NEW_URL, form(**data), follow=follow)

    def test_the_form(self):
        response = self.client.get(NEW_URL)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("klick.adx.se/exempelror/", html)
        self.assertIn(f'<option value="{self.quote_page.pk}">', html)
        self.assertNotIn(f'<option value="{self.draft.pk}">', html)
        self.assertIn('data-ln-show-when="mal=extern"', html)

    def test_a_link_to_a_flamingo_page_with_the_slug_made_from_the_description(self):
        response = self.post(mal="lp", kampanj=str(self.quote_page.pk))
        link = TrackedLink.objects.get(account=self.account, utskick__isnull=True)
        self.assertRedirects(response, reverse("flamingo:app_link", args=[link.pk]))
        self.assertEqual(link.slug, "affisch-i-verkstaden")
        self.assertEqual(link.kind, TrackedLink.Kind.LP)
        self.assertEqual(link.campaign_id, self.quote_page.pk)
        self.assertEqual(link.destination, landing_page_url(self.quote_page))
        self.assertEqual(link.label, "Affisch i verkstaden")
        self.assertTrue(link.is_named)

    def test_a_link_to_the_own_site_needs_no_review(self):
        self.post(adress="https://www.exempelror.example/vinter?gclid=x", slug="Vinter")
        link = TrackedLink.objects.get(account=self.account, slug="vinter")
        self.assertEqual(link.kind, TrackedLink.Kind.EXTERNAL)
        self.assertEqual(link.destination, "https://www.exempelror.example/vinter")
        self.assertFalse(AllowedHost.objects.filter(account=self.account).exists())
        self.assertEqual(mail.outbox, [])

    def test_a_new_host_waits_for_adx_and_the_agency_is_alerted(self):
        response = self.post(adress="https://annan-sajt.example/boka", follow=True)
        host = AllowedHost.objects.get(account=self.account, host="annan-sajt.example")
        self.assertEqual(host.status, AllowedHost.Status.PENDING)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["byran@adx.example"])
        self.assertIn(links.PENDING_TEXT, response.content.decode())
        link = TrackedLink.objects.get(account=self.account, utskick__isnull=True)
        self.assertFalse(links.destination_ok(link))

    def test_a_refused_host_is_an_error(self):
        AllowedHost.objects.create(
            account=self.account, host="nekad.example", status=AllowedHost.Status.REFUSED
        )
        response = self.post(adress="https://nekad.example/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("ADX har inte godkänt länkar till nekad.example.", response.content.decode())
        self.assertFalse(TrackedLink.objects.filter(account=self.account, slug__gt="").exists())

    def test_the_e8_rules(self):
        cases = {
            "": link_views.ADDRESS_TEXT,
            "/vinter": links.ABSOLUTE_TEXT,
            "https://bit.ly/abc": links.SHORTENER_TEXT,
            "https://192.168.0.1/": links.IP_TEXT,
            "https://exempelror.example:8443/": links.PORT_TEXT,
            "https://anna:hemligt@exempelror.example/": links.USERINFO_TEXT,
            "https://www.google.com/url?q=https://ond.example": links.REDIRECT_TEXT,
            "https://l.facebook.com/l.php?u=x": links.REDIRECT_TEXT,
            "https://klick.adx.se/m/abc": links.LINK_HOST_TEXT,
        }
        for address, text in cases.items():
            with self.subTest(address=address):
                response = self.post(adress=address, slug="prov")
                self.assertEqual(response.status_code, 400)
                self.assertIn(text, response.content.decode())
        self.assertFalse(TrackedLink.objects.filter(account=self.account, slug="prov").exists())

    def test_the_slug(self):
        response = self.post(slug="vin ter", adress="https://exempelror.example/")
        self.assertEqual(response.status_code, 400)
        self.assertIn(links.NAMED_SLUG_TEXT, response.content.decode())
        self.post(slug="vinter", adress="https://exempelror.example/")
        response = self.post(slug="Vinter", adress="https://exempelror.example/a")
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "Adressen klick.adx.se/exempelror/vinter finns redan. Välj en annan.",
            response.content.decode(),
        )
        self.assertEqual(TrackedLink.objects.filter(account=self.account, slug="vinter").count(), 1)
        # Samma slug hos ett annat konto är en annan adress.
        make_named(self.other_account, slug="vinter")
        self.assertEqual(TrackedLink.objects.filter(slug="vinter").count(), 2)

    def test_a_description_is_needed(self):
        response = self.post(beskrivning="  ", slug="", adress="https://exempelror.example/")
        self.assertEqual(response.status_code, 400)
        self.assertIn(link_views.LABEL_TEXT, response.content.decode())

    def test_another_accounts_campaign_is_400(self):
        service = Service.objects.create(account=self.other_account, name="Hemligt")
        foreign = Campaign.objects.create(
            account=self.other_account,
            service=service,
            name="Hemligt",
            status=Campaign.STATUS_LIVE,
            page={"title": "Hemligt"},
        )
        response = self.post(mal="lp", kampanj=str(foreign.pk))
        self.assertEqual(response.status_code, 400)
        response = self.post(mal="lp", kampanj="x")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TrackedLink.objects.filter(account=self.account, slug__gt="").exists())

    def test_an_unpublished_page_is_refused(self):
        response = self.post(mal="lp", kampanj=str(self.draft.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn(link_views.CAMPAIGN_TEXT, response.content.decode())

    def test_at_most_max_named(self):
        with mock.patch.object(link_views, "MAX_NAMED", 1):
            self.post(slug="ett", adress="https://exempelror.example/")
            response = self.post(slug="tva", adress="https://exempelror.example/")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TrackedLink.objects.filter(account=self.account, slug="tva").exists())

    def test_the_demo_changes_nothing(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        response = self.post(adress="https://annan-sajt.example/")
        self.assertRedirects(response, reverse("flamingo:app_links"))
        self.assertFalse(TrackedLink.objects.filter(account=self.account, slug__gt="").exists())
        self.assertFalse(AllowedHost.objects.exists())

    def test_staff_in_view_as_creates_for_real_and_is_logged(self):
        staff = self.client_for(self.staff)
        with self.assertLogs("apps.utskick.app_views.links", "INFO") as logs:
            self.post(client=staff, adress="https://exempelror.example/", slug="byran")
        link = TrackedLink.objects.get(account=self.account, slug="byran")
        self.assertIn(f"användare {self.staff.pk}, byrån i kundvyn", logs.output[0])
        self.assertIn(f"namngiven länk {link.pk} skapad", logs.output[0])

    def test_get_changes_nothing_and_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.anna)
        response = client.post(NEW_URL, form(adress="https://exempelror.example/"))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(TrackedLink.objects.filter(account=self.account, slug__gt="").exists())


# ---------------------------------------------------------------------------
# En länk
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS, **AGENCY)
class LinkPageTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)
        self.named = make_named(
            self.account, kind="lp", campaign=self.quote_page, destination="https://x"
        )
        self.url = reverse("flamingo:app_link", args=[self.named.pk])

    def test_the_page(self):
        now = timezone.now()
        Click.objects.create(
            account=self.account, link=self.named, channel="named", at=now, lp_visits=1
        )
        Click.objects.create(
            account=self.account, link=self.named, channel="named", at=now - timedelta(days=9)
        )
        Click.objects.create(account=self.account, link=self.named, channel="named", kind="scanner")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        numbers = response.context["numbers"]
        self.assertEqual(
            (numbers["clicks"], numbers["week"], numbers["visits"], numbers["leads"]), (2, 1, 1, 0)
        )
        html = response.content.decode()
        self.assertIn("https://klick.adx.se/exempelror/vinter", html)
        self.assertIn(reverse("flamingo:app_link_qr", args=[self.named.pk, "svg"]), html)
        self.assertIn(reverse("flamingo:app_link_qr", args=[self.named.pk, "png"]), html)
        self.assertIn('data-ln-copy="https://klick.adx.se/exempelror/vinter"', html)
        self.assertIn("Ladda ner SVG", html)

    def test_the_page_shows_the_whole_target(self):
        # Granskningen: målet kortades av mitt i ett ord, också på länkens sida.
        long = "https://exempelror.example/" + "boka-tid-for-service-av-var-" * 4
        TrackedLink.objects.filter(pk=self.named.pk).update(kind="external", destination=long)
        html = self.client.get(self.url).content.decode()
        self.assertIn(f'Går till <span class="fl-ut-url">{long}</span>', html)

    def test_save_changes_the_target_but_never_the_slug(self):
        response = self.client.post(
            self.url,
            {
                "action": "save",
                "beskrivning": "Skylten vid kassan",
                "mal": "extern",
                "adress": "https://exempelror.example/kassan",
                "slug": "annan",
            },
        )
        self.assertRedirects(response, self.url)
        self.named.refresh_from_db()
        self.assertEqual(self.named.slug, "vinter")
        self.assertEqual(self.named.label, "Skylten vid kassan")
        self.assertEqual(self.named.kind, TrackedLink.Kind.EXTERNAL)
        self.assertIsNone(self.named.campaign_id)
        self.assertEqual(self.named.destination, "https://exempelror.example/kassan")

    def test_a_bad_save_keeps_the_link(self):
        response = self.client.post(
            self.url, {"action": "save", "beskrivning": "X", "mal": "extern", "adress": "/x"}
        )
        self.assertEqual(response.status_code, 400)
        self.named.refresh_from_db()
        self.assertEqual(self.named.kind, TrackedLink.Kind.LP)

    def test_delete(self):
        click = Click.objects.create(account=self.account, link=self.named, channel="named")
        response = self.client.post(self.url, {"action": "delete"})
        self.assertRedirects(response, reverse("flamingo:app_links"))
        self.assertFalse(TrackedLink.objects.filter(pk=self.named.pk).exists())
        click.refresh_from_db()
        self.assertIsNone(click.link_id)

    def test_an_utskicks_own_link_and_another_accounts_link_are_404(self):
        foreign = make_named(self.other_account, slug="hemlig")
        for pk in (self.lp.pk, foreign.pk):
            with self.subTest(pk=pk):
                for name, args in (
                    ("flamingo:app_link", [pk]),
                    ("flamingo:app_link_qr", [pk, "svg"]),
                    ("flamingo:app_link_qr", [pk, "png"]),
                ):
                    self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404)
                self.client.post(reverse("flamingo:app_link", args=[pk]), {"action": "delete"})
        self.assertTrue(TrackedLink.objects.filter(pk=foreign.pk).exists())
        self.assertTrue(TrackedLink.objects.filter(pk=self.lp.pk).exists())

    def test_the_demo_changes_nothing(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.client.post(self.url, {"action": "delete"})
        self.assertTrue(TrackedLink.objects.filter(pk=self.named.pk).exists())


# ---------------------------------------------------------------------------
# Listan
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class LinkListTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)

    def test_named_links_with_their_chips_numbers_and_state(self):
        page = make_named(
            self.account, slug="vinter", kind="lp", campaign=self.quote_page, destination="x"
        )
        script = make_named(
            self.account, slug="ig-okt", destination="https://exempelror.example/boka"
        )
        pending = make_named(self.account, slug="ny", destination="https://ny-sajt.example/")
        AllowedHost.objects.create(account=self.account, host="ny-sajt.example")
        SiteSnippet.objects.create(
            account=self.account, domain="exempelror.example", last_seen_at=timezone.now()
        )
        Click.objects.create(account=self.account, link=page, channel="named")
        Click.objects.create(account=self.account, link=script, channel="named")
        TrackedLink.objects.filter(pk=script.pk).update(human_clicks=131)
        Lead.objects.create(
            account=self.account,
            campaign=self.quote_page,
            source=Lead.SOURCE_FORM,
            attribution={"channel": "named", "link": page.pk, "click": 1},
        )
        response = self.client.get(reverse("flamingo:app_links"))
        self.assertEqual(response.status_code, 200)
        rows = {row["link"].slug: row for row in response.context["named_rows"]}
        self.assertEqual((rows["vinter"]["clicks"], rows["vinter"]["leads"]), (1, 1))
        self.assertEqual(rows["vinter"]["chip_label"], "Flamingo-sida")
        self.assertEqual((rows["ig-okt"]["clicks"], rows["ig-okt"]["leads"]), (131, None))
        self.assertEqual(rows["ig-okt"]["chip_label"], "Skript finns")
        self.assertEqual(rows["ny"]["chip_label"], "Extern")
        self.assertEqual(rows["ny"]["state_text"], links.PENDING_TEXT)
        self.assertEqual(rows["vinter"]["text"], "klick.adx.se/exempelror/vinter")
        html = response.content.decode()
        self.assertIn(link_views.EXPLAINER, html)
        self.assertIn("exempelror.example/boka", html)
        self.assertIn(links.PENDING_TEXT, html)
        self.assertIn(f'href="{reverse("flamingo:app_link", args=[pending.pk])}#qr"', html)

    def test_each_utskick_is_one_row_with_its_personal_links(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(frozen_at=timezone.now())
        self.click("Lp0001")
        Lead.objects.create(
            account=self.account,
            campaign=self.quote_page,
            source=Lead.SOURCE_FORM,
            utskick=self.utskick,
        )
        response = self.client.get(reverse("flamingo:app_links"))
        rows = response.context["personal_rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["utskick"], self.utskick)
        self.assertEqual(row["code"], "k.adx.se/Lp0001")
        # Granskningen: "+ N" räknade koder (tre länkar till en mottagare gav
        # "+ 2"); det är en per mottagare, som i mockupen.
        self.assertEqual(row["more"], 0)
        self.assertEqual((row["clicks"], row["leads"]), (1, 1))
        self.assertEqual(
            [d["label"] for d in row["destinations"]], ["Flamingo-sida", "Extern", "Extern"]
        )
        html = response.content.decode()
        self.assertNotIn("k.adx.se/Lp0001 +", html)
        self.assertIn(reverse("flamingo:app_utskick", args=[self.utskick.pk]), html)
        bo = make_contact(self.account, first_name="Bo", phone="+46701234599")
        second = Recipient.objects.create(
            utskick=self.utskick, contact=bo, channel="sms", address=bo.phone, status="delivered"
        )
        for code, link in (("Lp0002", self.lp), ("Ex0002", self.ext), ("Tr0002", self.third)):
            LinkCode.objects.create(
                code=code,
                kind=LinkCode.Kind.LINK,
                account=self.account,
                value_hash="b",
                recipient=second,
                link=link,
            )
        html = self.client.get(reverse("flamingo:app_links")).content.decode()
        self.assertIn("k.adx.se/Lp0001 + 1", html)

    def test_an_email_only_utskick_says_how_many_got_a_personal_link(self):
        mail = Utskick.objects.create(
            account=self.account,
            name="Höstbrevet",
            status=Utskick.Status.SENT,
            channel_mode=Utskick.ChannelMode.EMAIL_ONLY,
            frozen_at=timezone.now(),
        )
        TrackedLink.objects.create(
            account=self.account,
            utskick=mail,
            kind="external",
            key="boka",
            destination="https://exempelror.example/" + "boka-tid-for-service-av-var-" * 4,
        )
        for n, status in enumerate(("delivered", "delivered", "skipped")):
            kontakt = make_contact(self.account, first_name=f"M{n}", email=f"m{n}@kund.example")
            Recipient.objects.create(
                utskick=mail, contact=kontakt, channel="email", address=kontakt.email, status=status
            )
        response = self.client.get(reverse("flamingo:app_links"))
        row = next(r for r in response.context["personal_rows"] if r["utskick"] == mail)
        self.assertEqual(row["code"], "klick.adx.se/m/...")
        self.assertEqual(row["kind_text"], "personlig länk i mejlet till 2 mottagare")
        # Granskningen: ett avkortat mål slutar med tre punkter.
        dest = row["destinations"][0]["text"]
        self.assertTrue(dest.endswith("..."), dest)
        self.assertLessEqual(len(dest), link_views.SHORT_MAX)

    def test_a_draft_has_no_personal_links_and_other_accounts_never_show(self):
        draft = Utskick.objects.create(account=self.account, name="Utkastet")
        TrackedLink.objects.create(
            account=self.account, utskick=draft, kind="external", key="x", destination="https://a"
        )
        foreign = make_named(self.other_account, slug="hemlig")
        response = self.client.get(reverse("flamingo:app_links"))
        self.assertNotIn(draft, [r["utskick"] for r in response.context["personal_rows"]])
        self.assertNotIn(foreign, [r["link"] for r in response.context["named_rows"]])
        self.assertNotIn("hemlig", response.content.decode())

    def test_every_cell_has_a_label(self):
        make_named(self.account)
        Utskick.objects.filter(pk=self.utskick.pk).update(frozen_at=timezone.now())
        html = self.client.get(reverse("flamingo:app_links")).content.decode()
        self.assertIn("<thead>", html)
        self.assertNotRegex(html, r"<td(?![^>]*data-label=)[^>]*>")


# ---------------------------------------------------------------------------
# QR-koderna
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS, SITE_BASE_URL="https://adx.example")
class QrTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)
        self.named = make_named(self.account)

    def test_svg_and_png_for_a_named_link(self):
        url = "https://klick.adx.se/exempelror/vinter"
        svg = self.client.get(reverse("flamingo:app_link_qr", args=[self.named.pk, "svg"]))
        self.assertEqual(svg.status_code, 200)
        self.assertEqual(svg["Content-Type"], "image/svg+xml")
        self.assertEqual(svg.content, qr.svg(url))
        self.assertTrue(svg["Content-Disposition"].startswith("inline;"))
        png = self.client.get(
            reverse("flamingo:app_link_qr", args=[self.named.pk, "png"]) + "?ladda=1"
        )
        self.assertEqual(png.content, qr.png(url))
        self.assertEqual(
            png["Content-Disposition"], 'attachment; filename="qr-exempelror-vinter.png"'
        )
        self.assertEqual(png["X-Content-Type-Options"], "nosniff")

    def test_the_signup_page(self):
        expected = optin.absolute(reverse("utskick_public:signup", args=["exempelror"]))
        self.assertEqual(expected, "https://adx.example/utskick/exempelror/")
        response = self.client.get(reverse("flamingo:app_signup_qr", args=["png"]) + "?ladda=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, qr.png(expected))
        self.assertIn("qr-anmalan-exempelror.png", response["Content-Disposition"])
        svg = self.client.get(reverse("flamingo:app_signup_qr", args=["svg"]))
        self.assertEqual(svg.content, qr.svg(expected))

    def test_the_signup_settings_link_the_codes(self):
        html = self.client.get(reverse("flamingo:app_signup")).content.decode()
        self.assertIn(reverse("flamingo:app_signup_qr", args=["svg"]) + "?ladda=1", html)
        self.assertIn(reverse("flamingo:app_signup_qr", args=["png"]) + "?ladda=1", html)

    def test_off_is_404(self):
        from .models import UtskickSettings

        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        for url in (
            reverse("flamingo:app_signup_qr", args=["svg"]),
            reverse("flamingo:app_link_qr", args=[self.named.pk, "png"]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)


# ---------------------------------------------------------------------------
# Klicket på klick.adx.se/<public_slug>/<slug>
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS, **AGENCY)
class NamedClickTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.page = make_named(
            self.account, slug="vinter", kind="lp", campaign=self.quote_page, destination="x"
        )
        self.site = make_named(
            self.account, slug="karta", destination="https://exempelror.example/karta?vy=1#hitta"
        )

    def get(self, path, agent=IPHONE, method="get", **extra):
        client = Client(enforce_csrf_checks=True)
        return getattr(client, method)(path, HTTP_USER_AGENT=agent, **KLICK, **extra)

    def assertNoCookies(self, response):
        self.assertEqual(response.cookies, {})
        self.assertNotIn("Set-Cookie", response.headers)

    def test_a_click_on_a_flamingo_page_gives_ut_and_is_saved_as_named(self):
        response = self.get("/exempelror/vinter")
        self.assertEqual(response.status_code, 302)
        self.assertNoCookies(response)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])
        location = response["Location"]
        self.assertTrue(location.startswith(landing_page_url(self.quote_page)))
        click = Click.objects.get(link=self.page)
        self.assertEqual(query(location), {"ut": tokens.ut_token(click.pk)})
        self.assertEqual(click.channel, Click.Channel.NAMED)
        self.assertEqual(click.kind, Click.Kind.HUMAN)
        self.assertIsNone(click.recipient_id)
        self.assertIsNone(click.contact_id)
        self.assertIsNone(click.utskick_id)
        self.assertEqual(click.account_id, self.account.pk)

    def test_an_external_link_keeps_its_query_and_fragment(self):
        response = self.get("/exempelror/karta")
        self.assertEqual(response["Location"], "https://exempelror.example/karta?vy=1#hitta")
        self.assertEqual(Click.objects.filter(link=self.site).count(), 1)

    def test_head_records_nothing(self):
        response = self.get("/exempelror/vinter", method="head")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("ut=", response["Location"])
        self.assertFalse(Click.objects.exists())

    def test_bots_are_counted_not_stored(self):
        for agent in ("WhatsApp/2.23", "facebookexternalhit/1.1", ""):
            with self.subTest(agent=agent):
                response = self.get("/exempelror/vinter", agent=agent)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("ut=", response["Location"])
        self.assertFalse(Click.objects.exists())
        self.page.refresh_from_db()
        self.assertEqual(self.page.bot_hits, 3)

    def test_twenty_rows_per_visitor_and_hour_then_repeats(self):
        for _ in range(23):
            self.get("/exempelror/vinter")
        rows = Click.objects.filter(link=self.page)
        self.assertEqual(rows.count(), 20)
        self.assertEqual(rows.order_by("-at", "-pk").first().repeat_count, 3)

    def test_unknown_account_slug_and_utskick_links_are_the_same_404(self):
        TrackedLink.objects.filter(pk=self.lp.pk).update(slug="boka")
        make_named(self.other_account, slug="hemlig")
        paths = ("/okand/vinter", "/exempelror/okand", "/exempelror/boka", "/exempelror/hemlig")
        for path in paths:
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertIn("Länken har gått ut", response.content.decode())
        self.assertFalse(Click.objects.exists())

    def test_a_pending_or_refused_host_is_checked_at_every_click(self):
        link = make_named(self.account, slug="ny", destination="https://ny-sajt.example/")
        row = AllowedHost.objects.create(account=self.account, host="ny-sajt.example")
        self.assertEqual(self.get("/exempelror/ny").status_code, 404)
        row.status = AllowedHost.Status.APPROVED
        row.save()
        self.assertEqual(self.get("/exempelror/ny")["Location"], "https://ny-sajt.example/")
        row.status = AllowedHost.Status.REFUSED
        row.save()
        self.assertEqual(self.get("/exempelror/ny").status_code, 404)
        self.assertEqual(Click.objects.filter(link=link).count(), 1)

    def test_only_on_the_email_host(self):
        client = Client()
        self.assertEqual(client.get("/exempelror/vinter", HTTP_HOST="k.adx.se").status_code, 404)
        self.assertEqual(client.get("/exempelror/vinter").status_code, 404)
        self.assertEqual(self.get("/exempelror/vinter", method="post").status_code, 405)
        self.assertFalse(Click.objects.exists())

    def test_a_disabled_account_still_redirects(self):
        from .models import UtskickSettings

        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        self.assertEqual(self.get("/exempelror/vinter").status_code, 302)

    def test_a_lead_through_the_link_is_counted_on_the_link_not_the_utskick(self):
        response = self.get("/exempelror/vinter")
        ut = query(response["Location"])["ut"]
        lp = Client().post(
            self.quote_page.landing_url,
            {"name": "Bo", "phone": "070-174 06 09", "email": "", "q_storlek": "6", "ut": ut},
        )
        self.assertEqual(lp.status_code, 302)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertIsNone(lead.utskick_id)
        self.assertIsNone(lead.contact_id)
        self.assertEqual(lead.attribution["channel"], "named")
        self.assertEqual(lead.attribution["link"], self.page.pk)
        self.assertFalse(lead.can_send_to_google)
        links.rollup(timezone.now())
        self.page.refresh_from_db()
        self.assertEqual((self.page.human_clicks, self.page.leads), (1, 1))
        html = self.client_for(self.anna).get(reverse("flamingo:app_links")).content.decode()
        self.assertIn("klick.adx.se/exempelror/vinter", html)


# ---------------------------------------------------------------------------
# adx= till kundens egen sajt (E.3, E.6)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class AdxParameterTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.named = make_named(self.account, destination="https://www.exempelror.example/boka")
        self.snippet = SiteSnippet.objects.create(account=self.account, domain="exempelror.example")

    def named_click(self):
        response = Client().get("/exempelror/vinter", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(response.status_code, 302)
        return response["Location"]

    def test_no_adx_until_the_script_has_been_seen(self):
        self.assertNotIn("adx=", self.named_click())
        SiteSnippet.objects.filter(pk=self.snippet.pk).update(last_seen_at=timezone.now())
        location = self.named_click()
        click = Click.objects.filter(link=self.named).order_by("-pk").first()
        self.assertEqual(query(location), {"adx": tokens.adx_token(click.pk)})
        self.assertEqual(urlsplit(location).path, "/boka")

    def test_only_to_the_snippet_domain_and_its_subdomains(self):
        SiteSnippet.objects.filter(pk=self.snippet.pk).update(last_seen_at=timezone.now())
        for destination, wanted in (
            ("https://exempelror.example/", True),
            ("https://butik.exempelror.example/", True),
            ("https://exempelror.example.annan.example/", False),
            ("https://inteexempelror.example/", False),
        ):
            with self.subTest(destination=destination):
                link = TrackedLink(
                    account=self.account, kind="external", destination=destination, slug="x"
                )
                self.assertEqual(links.adx_wanted(link), wanted)
        other = SiteSnippet.objects.create(
            account=self.other_account, domain="annanfirma.example", last_seen_at=timezone.now()
        )
        link = TrackedLink(
            account=self.account, kind="external", destination="https://annanfirma.example/"
        )
        self.assertFalse(links.adx_wanted(link))
        self.assertTrue(other.is_installed)

    def test_an_sms_click_to_the_site_gets_adx_too(self):
        SiteSnippet.objects.filter(pk=self.snippet.pk).update(last_seen_at=timezone.now())
        response = Client().get("/Ex0001", HTTP_USER_AGENT=IPHONE, HTTP_HOST="k.adx.se")
        params = query(response["Location"])
        click = Click.objects.get(link=self.ext)
        self.assertEqual(params["adx"], tokens.adx_token(click.pk))
        self.assertEqual(params["utm_source"], "flamingo")
        self.assertEqual(params["vy"], "1")
        # add_utm av: målet som det är, plus adx.
        response = Client().get("/Tr0001", HTTP_USER_AGENT=IPHONE, HTTP_HOST="k.adx.se")
        click = Click.objects.get(link=self.third)
        self.assertEqual(query(response["Location"]), {"adx": tokens.adx_token(click.pk)})

    def test_a_pasted_adx_is_removed_from_the_address(self):
        # Granskningen: en adress med en mottagares adx klistrad in gav varje
        # besök utan sparat klick (testsms, bottar) till den mottagaren.
        pasted = f"https://exempelror.example/boka?adx={tokens.adx_token(7)}&vy=2&ut=kvar"
        self.assertEqual(
            links.clean_external(self.account, pasted),
            "https://exempelror.example/boka?vy=2&ut=kvar",
        )

    def test_never_for_bots_head_or_flamingo_pages(self):
        SiteSnippet.objects.filter(pk=self.snippet.pk).update(last_seen_at=timezone.now())
        bot = Client().get("/exempelror/vinter", HTTP_USER_AGENT="WhatsApp/2", **KLICK)
        self.assertNotIn("adx=", bot["Location"])
        head = Client().head("/exempelror/vinter", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertNotIn("adx=", head["Location"])
        response = Client().get("/Lp0001", HTTP_USER_AGENT=IPHONE, HTTP_HOST="k.adx.se")
        self.assertNotIn("adx", query(response["Location"]))
