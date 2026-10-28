"""
Menyn med sex punkter (app_nav.py, mockups/flamingo-meny.html, 2026-10-28).

Steg 1 av 2: menyn, flikarna, den samlade inställningssidan, Inkorgens chips
och Att göra på översikten. Inga adresser ändras: varje gammal adress ska
svara som förut (eller skicka till rätt del av inställningssidan).
"""

import re
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.sms.models import SmsAccount
from apps.utskick.models import (
    ContactList,
    Switchboard,
    Thread,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from apps.utskick.testing import UtskickFixture, make_contact

from . import app_nav
from .models import Campaign, FlamingoAccount, LandingPage, Lead, Service

SIX = ["Översikt", "Inkorg", "Landningssidor", "Annonser", "Sms och e-post", "Företaget"]


def nav_html(html):
    return html.split('<nav class="fl-app-nav"', 1)[1].split("</nav>", 1)[0]


def tabs_html(html):
    if 'class="fl-tabs fl-app-tabs"' not in html:
        return ""
    return html.split('class="fl-tabs fl-app-tabs"', 1)[1].split("</nav>", 1)[0]


class NavFixture(UtskickFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.service = Service.objects.create(account=cls.account, name="Rörjour")
        cls.campaign = Campaign.objects.create(
            account=cls.account, service=cls.service, name="Rörjour Nacka"
        )
        cls.page = LandingPage.objects.create(account=cls.account, name="Avloppsspolning")
        cls.lead = Lead.objects.create(account=cls.account, name="Mikael Ström")
        cls.lista = ContactList.objects.create(account=cls.account, name="Kunder")
        cls.link = TrackedLink.objects.create(
            account=cls.account,
            slug="affisch",
            kind=TrackedLink.Kind.EXTERNAL,
            destination="https://exempelror.example/",
            label="Affisch i verkstaden",
        )
        cls.utskick = Utskick.objects.create(account=cls.account, name="Höstservice")

    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)
        self.kontakt = make_contact(self.account, first_name="Anna", last_name="Lind")


class MainMenuTests(NavFixture, TestCase):
    def test_six_items_in_order_with_exact_labels(self):
        self.assertEqual([item.label for item in app_nav.MAIN_NAV], SIX)
        response = self.client.get(reverse("flamingo:app"))
        self.assertEqual([item.label for item in response.context["app_nav"]], SIX)
        menu = nav_html(response.content.decode())
        labels = [
            re.sub(r"<[^>]+>", "", part.split("</span><", 1)[0].split("</span>\n", 1)[0])
            .replace("&shy;", "")
            .replace("</span>", "")
            .strip()
            for part in menu.split('<span class="fl-app-nav__label">')[1:]
        ]
        self.assertEqual(labels, SIX)
        # Landningssidor får brytas i den smala raden längst ner, och
        # "e-post" bryts aldrig vid bindestrecket ("Sms och e- / post").
        self.assertIn("Landnings&shy;sidor", menu)
        self.assertIn('Sms och <span class="fl-nowrap">e-post</span>', menu)
        hrefs = [part.split('"', 1)[0] for part in menu.split('href="')[1:]]
        self.assertEqual(hrefs, [item.url for item in app_nav.MAIN_NAV])

    def test_every_item_has_an_icon_and_the_inbox_its_badge(self):
        html = nav_html(self.client.get(reverse("flamingo:app")).content.decode())
        self.assertEqual(html.count('class="fl-app-nav__icon"'), 6)
        self.assertIn('class="fl-app-nav__badge" aria-label="1 nya"', html)

    def test_without_utskick_sms_och_e_post_is_hidden(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        response = self.client.get(reverse("flamingo:app"))
        labels = [item.label for item in response.context["app_nav"]]
        self.assertEqual(labels, [label for label in SIX if label != "Sms och e-post"])
        menu = nav_html(response.content.decode())
        self.assertNotIn("Sms och e-post", menu)
        self.assertNotIn(reverse("flamingo:app_utskick_list"), menu)
        # Delens sidor finns inte för kontot (utskick_view).
        for name in ("flamingo:app_utskick_list", "flamingo:app_contacts", "flamingo:app_links"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 404, name)

    def test_staff_viewing_as_the_customer_sees_the_same_menu(self):
        response = self.client_for(self.staff).get(reverse("flamingo:app_inbox"))
        self.assertEqual([item.label for item in response.context["app_nav"]], SIX)
        self.assertEqual(response.context["app_active"], "inbox")

    def test_an_unknown_page_key_is_an_error(self):
        with self.assertRaises(ValueError):
            app_nav.split_key("contacts")
        self.assertEqual(app_nav.split_key("ads/google"), ("ads", "google"))
        self.assertEqual(app_nav.split_key(""), ("", ""))


class HighlightTests(NavFixture, TestCase):
    """Varje sida markerar rätt punkt i menyn och rätt flik under rubriken."""

    def pages(self):
        u, k = self.utskick, self.kontakt
        return [
            # (adress, punkt, flik)
            (reverse("flamingo:app"), "overview", ""),
            (reverse("flamingo:app_inbox"), "inbox", ""),
            (reverse("flamingo:app_lead", args=[self.lead.pk]), "inbox", ""),
            (reverse("flamingo:app_pages"), "pages", ""),
            (reverse("flamingo:app_page", args=[self.page.pk]), "pages", ""),
            (reverse("flamingo:app_page_answers", args=[self.page.pk]), "pages", ""),
            (reverse("flamingo:app_campaigns"), "ads", "campaigns"),
            (reverse("flamingo:app_campaign_new"), "ads", "campaigns"),
            (reverse("flamingo:app_campaign", args=[self.campaign.pk]), "ads", "campaigns"),
            (reverse("flamingo:app_google"), "ads", "google"),
            (reverse("flamingo:app_utskick_list"), "messaging", "sent"),
            (reverse("flamingo:app_utskick", args=[u.pk]), "messaging", "sent"),
            (reverse("flamingo:app_utskick_step", args=[u.pk, "mottagare"]), "messaging", "sent"),
            (reverse("flamingo:app_utskick_recipients", args=[u.pk]), "messaging", "sent"),
            (reverse("flamingo:app_contacts"), "messaging", "contacts"),
            (reverse("flamingo:app_contact", args=[k.pk]), "messaging", "contacts"),
            (reverse("flamingo:app_contact_new"), "messaging", "contacts"),
            (reverse("flamingo:app_import"), "messaging", "contacts"),
            (reverse("flamingo:app_fields"), "messaging", "contacts"),
            (reverse("flamingo:app_signup"), "messaging", "contacts"),
            (reverse("flamingo:app_lists"), "messaging", "lists"),
            (reverse("flamingo:app_list", args=[self.lista.pk]), "messaging", "lists"),
            (reverse("flamingo:app_segment_new"), "messaging", "lists"),
            (reverse("flamingo:app_links"), "messaging", "links"),
            (reverse("flamingo:app_link_new"), "messaging", "links"),
            (reverse("flamingo:app_link", args=[self.link.pk]), "messaging", "links"),
            (reverse("flamingo:app_utskick_health"), "messaging", "health"),
            (reverse("flamingo:app_business"), "business", "details"),
            (reverse("flamingo:app_reviews"), "business", "details"),
            (reverse("flamingo:app_media"), "business", "media"),
            (reverse("flamingo:app_settings"), "business", "settings"),
            (reverse("flamingo:app_dpa"), "business", "settings"),
            (reverse("flamingo:app_utskick_snippet"), "business", "settings"),
            (reverse("flamingo:app_utskick_domain"), "business", "settings"),
        ]

    def test_every_page_highlights_its_item_and_tab(self):
        for client in (self.client, self.client_for(self.staff)):
            for url, section, tab in self.pages():
                with self.subTest(url=url, staff=client is not self.client):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context["app_active"], section)
                    self.assertEqual(response.context["app_tab"], tab)
                    html = response.content.decode()
                    if '<nav class="fl-app-nav"' not in html:
                        # Sidbyggaren har ingen meny (hela skärmen).
                        continue
                    item = next(i for i in app_nav.MAIN_NAV if i.key == section)
                    self.assertIn(
                        f'href="{item.url}" aria-current="page"',
                        nav_html(html),
                    )
                    self.assertEqual(nav_html(html).count('aria-current="page"'), 1)
                    row = tabs_html(html)
                    if row and tab:
                        current = next(t for t in response.context["app_tabs"] if t["current"])
                        self.assertEqual(current["key"], tab)
                        self.assertIn(f'href="{current["url"]}" aria-current="page"', row)
                        self.assertEqual(row.count('aria-current="page"'), 1)

    def test_the_tab_rows_are_the_mockups(self):
        cases = {
            reverse("flamingo:app_campaigns"): ["Kampanjer", "Google-kontot"],
            reverse("flamingo:app_utskick_list"): [
                "Skickade",
                "Kontakter",
                "Listor och segment",
                "Länkar och QR",
                "Leveranshälsa",
            ],
            reverse("flamingo:app_business"): ["Uppgifter och tjänster", "Media", "Inställningar"],
        }
        for url, labels in cases.items():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual([t["label"] for t in response.context["app_tabs"]], labels)
                row = tabs_html(response.content.decode())
                for label in labels:
                    self.assertIn(f">{label}</a>", row)

    def test_sections_without_tabs_have_no_tab_row(self):
        for name in ("flamingo:app", "flamingo:app_inbox", "flamingo:app_pages"):
            with self.subTest(name=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertNotIn("fl-app-tabs", html)

    def test_no_dropdown_sub_navigation_is_left(self):
        for name in ("flamingo:app_contacts", "flamingo:app_utskick_list", "flamingo:app_lists"):
            with self.subTest(name=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertNotIn("fl-subnav", html)
                self.assertNotIn("<details", tabs_html(html))
                self.assertIn('class="fl-tabs fl-app-tabs"', html)

    def test_sms_api_tab_only_with_an_sms_account(self):
        url = reverse("flamingo:app_utskick_list")
        response = self.client.get(url)
        self.assertNotIn("sms_api", [t["key"] for t in response.context["app_tabs"]])
        self.assertNotIn("Sms-API", tabs_html(response.content.decode()))
        SmsAccount.objects.create(customer=self.customer, is_enabled=False)
        response = self.client.get(url)
        self.assertNotIn("Sms-API", tabs_html(response.content.decode()))
        SmsAccount.objects.filter(customer=self.customer).update(is_enabled=True)
        row = tabs_html(self.client.get(url).content.decode())
        self.assertIn(f'href="{reverse("sms:dashboard")}">Sms-API</a>', row)


@override_settings(UTSKICK_EMAIL_LIVE=False)
class NewMessageButtonTests(NavFixture, TestCase):
    def latest(self):
        return Utskick.objects.filter(account=self.account).order_by("-pk").first()

    def test_nytt_sms_starts_the_flow_with_sms(self):
        response = self.client.post(reverse("flamingo:app_utskick_new"), {"kanal": "sms"})
        utskick = self.latest()
        self.assertEqual(utskick.channel_mode, Utskick.ChannelMode.SMS_ONLY)
        self.assertRedirects(
            response,
            reverse("flamingo:app_utskick_step", args=[utskick.pk, "mottagare"]),
            fetch_redirect_response=False,
        )

    def test_nytt_mejl_starts_the_flow_with_email(self):
        response = self.client.post(reverse("flamingo:app_utskick_new"), {"kanal": "e-post"})
        utskick = self.latest()
        self.assertEqual(utskick.channel_mode, Utskick.ChannelMode.EMAIL_ONLY)
        self.assertRedirects(
            response,
            reverse("flamingo:app_utskick_step", args=[utskick.pk, "mottagare"]),
            fetch_redirect_response=False,
        )

    def test_without_a_channel_the_flow_chooses_as_before(self):
        self.client.post(reverse("flamingo:app_utskick_new"))
        self.assertEqual(self.latest().channel_mode, Utskick.ChannelMode.SMS_ONLY)
        self.client.post(reverse("flamingo:app_utskick_new"), {"kanal": "fax"})
        self.assertEqual(self.latest().channel_mode, Utskick.ChannelMode.SMS_ONLY)

    def test_the_buttons_sit_on_the_tabs_first_pages(self):
        html = self.client.get(reverse("flamingo:app_utskick_list")).content.decode()
        self.assertIn('<input type="hidden" name="kanal" value="sms">', html)
        self.assertIn(">Nytt sms</button>", html)
        # E-posten är inte påslagen: ingen obyggd funktion i produkten.
        self.assertNotIn(">Nytt mejl</button>", html)
        html = self.client.get(reverse("flamingo:app_contacts")).content.decode()
        self.assertIn(">Nytt sms</button>", html)
        # Inte på en kontakts sida eller utanför delen.
        url = reverse("flamingo:app_contact", args=[self.kontakt.pk])
        self.assertNotIn(">Nytt sms</button>", self.client.get(url).content.decode())
        self.assertNotIn(
            ">Nytt sms</button>", self.client.get(reverse("flamingo:app")).content.decode()
        )

    def test_nytt_mejl_shows_when_email_is_on(self):
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK, defaults={"email_enabled": True}
        )
        with override_settings(UTSKICK_EMAIL_LIVE=True):
            html = self.client.get(reverse("flamingo:app_utskick_list")).content.decode()
        self.assertIn('<input type="hidden" name="kanal" value="e-post">', html)
        self.assertIn(">Nytt mejl</button>", html)

    def test_the_demo_has_both(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        html = self.client.get(reverse("flamingo:app_utskick_list")).content.decode()
        self.assertIn(">Nytt sms</button>", html)
        self.assertIn(">Nytt mejl</button>", html)


class SettingsPageTests(NavFixture, TestCase):
    url = reverse("flamingo:app_settings")

    def test_one_page_with_every_section(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "flamingo/app/onboarding/settings.html")
        html = response.content.decode()
        for anchor in ("aviseringar", "kontakter", "utskick", "sparningsskript", "inloggningar"):
            self.assertIn(f'id="{anchor}"', html)
            self.assertIn(f'href="#{anchor}"', html)
        # Varje del postar till sin gamla adress.
        for name in (
            "flamingo:app_settings",
            "flamingo:app_contacts_settings",
            "flamingo:app_utskick_settings",
        ):
            self.assertIn(f'action="{reverse(name)}"', html)
        self.assertIn("Spårningsskript på egen sajt", html)
        self.assertIn(reverse("flamingo:app_utskick_snippet"), html)

    def test_without_utskick_only_the_flamingo_sections(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="aviseringar"', html)
        self.assertIn('id="inloggningar"', html)
        for anchor in ("kontakter", "utskick", "sparningsskript"):
            self.assertNotIn(f'id="{anchor}"', html)
        for name in ("flamingo:app_contacts_settings", "flamingo:app_utskick_settings"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 404)

    def test_old_urls_go_to_their_section(self):
        for name, anchor in (
            ("flamingo:app_contacts_settings", "#kontakter"),
            ("flamingo:app_utskick_settings", "#utskick"),
        ):
            with self.subTest(name=name):
                for client in (self.client, self.client_for(self.staff)):
                    self.assertRedirects(
                        client.get(reverse(name)),
                        self.url + anchor,
                        fetch_redirect_response=False,
                    )

    def contact_data(self, **extra):
        data = {
            "consent_text_sms": "Ja, jag vill få erbjudanden från Exempelrör via sms.",
            "consent_text_email": "Ja, jag vill få nyheter från Exempelrör via e-post.",
            "lp_consent": "1",
            "privacy_url": "",
            "pref_email_note": "",
            "unsubscribe_text": "",
        }
        data.update(extra)
        return data

    def test_the_contact_form_still_saves_and_shows_its_errors_in_place(self):
        url = reverse("flamingo:app_contacts_settings")
        response = self.client.post(url, self.contact_data(pref_email_note="En gång i månaden."))
        self.assertRedirects(response, self.url + "#kontakter", fetch_redirect_response=False)
        row = UtskickSettings.objects.get(account=self.account)
        self.assertEqual(row.pref_email_note, "En gång i månaden.")
        response = self.client.post(url, self.contact_data(privacy_url="http://exempelror.example"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "flamingo/app/onboarding/settings.html")
        self.assertContains(response, "Adressen ska börja med https://")
        self.assertEqual(response.context["app_tab"], "settings")
        # Flamingos eget formulär postar till sin adress, inte till den här.
        self.assertContains(
            response, f'<form method="post" class="fl-form" action="{self.url}">', html=False
        )

    def window(self, **extra):
        data = {
            "weekday_start": "8",
            "weekday_end": "21",
            "weekend_start": "11",
            "weekend_end": "16",
            "weekly_cap_sms": "3",
            "weekly_cap_email": "2",
            "notify_on_reply": "1",
        }
        data.update(extra)
        return data

    def test_the_utskick_form_still_saves_and_shows_its_errors_in_place(self):
        url = reverse("flamingo:app_utskick_settings")
        response = self.client.post(url, self.window())
        self.assertRedirects(response, self.url + "#utskick", fetch_redirect_response=False)
        row = UtskickSettings.objects.get(account=self.account)
        self.assertEqual(row.weekly_cap_sms, 3)
        response = self.client.post(url, self.window(weekday_start="20", weekday_end="9"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Välj ett fönster för vardagar")
        self.assertContains(response, 'id="aviseringar"')

    def test_the_flamingo_form_still_saves_here(self):
        response = self.client.post(
            self.url,
            {"notify_phone": "070-123 45 67", "notify_sms": "1", "autoreply_text": ""},
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.account.refresh_from_db()
        self.assertTrue(self.account.notify_sms)


@override_settings(UTSKICK_EMAIL_LIVE=False)
class InboxChipTests(NavFixture, TestCase):
    def test_the_chips_are_the_mockups(self):
        response = self.client.get(reverse("flamingo:app_inbox"))
        labels = [t["label"] for t in response.context["types"]]
        self.assertEqual(labels, ["Alla", "Förfrågningar", "Svar på sms"])
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK, defaults={"email_enabled": True}
        )
        with override_settings(UTSKICK_EMAIL_LIVE=True):
            response = self.client.get(reverse("flamingo:app_inbox"))
        labels = [t["label"] for t in response.context["types"]]
        self.assertEqual(labels, ["Alla", "Förfrågningar", "Svar på sms", "Svar på mejl"])
        self.assertContains(response, "?typ=e-postsvar")

    def test_replies_and_inquiries_are_one_list(self):
        reply = Lead.objects.create(
            account=self.account, source=Lead.SOURCE_REPLY, name="Anna K", message="Funkar torsdag?"
        )
        Thread.objects.create(account=self.account, channel="sms", lead=reply, address="+4670")
        response = self.client.get(reverse("flamingo:app_inbox"))
        names = [lead.display_name for lead in response.context["leads"]]
        self.assertIn("Anna K", names)
        self.assertIn("Mikael Ström", names)
        response = self.client.get(reverse("flamingo:app_inbox") + "?typ=sms-svar")
        self.assertEqual([lead.display_name for lead in response.context["leads"]], ["Anna K"])


class TodoTests(NavFixture, TestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        Lead.objects.filter(pk=self.lead.pk).update(created_at=now - timedelta(days=3))
        Lead.objects.create(account=self.account, name="Lena Berg", created_at=now)

    def html(self):
        return self.client.get(reverse("flamingo:app")).content.decode()

    def test_unanswered_inquiries_with_the_oldests_age(self):
        html = self.html()
        self.assertIn(">Att göra</h2>", html)
        self.assertIn("<b>2 förfrågningar utan svar</b>", html)
        self.assertIn("från Mikael Ström och har väntat 3 dagar.", html)
        self.assertIn(reverse("flamingo:app_inbox") + "?typ=forfragningar&amp;status=new", html)

    def test_unanswered_replies(self):
        reply = Lead.objects.create(
            account=self.account,
            source=Lead.SOURCE_REPLY,
            name="Anna K",
            message="Funkar torsdag förmiddag?",
        )
        Thread.objects.create(
            account=self.account, channel="sms", lead=reply, utskick=self.utskick, address="+4670"
        )
        html = self.html()
        self.assertIn("<b>1 svar på ditt sms</b>", html)
        self.assertIn(
            "&quot;Funkar torsdag förmiddag?&quot; från Anna K, på utskicket Höstservice.", html
        )
        # Inkorgens förfrågningar räknar inte svaret.
        self.assertIn("<b>2 förfrågningar utan svar</b>", html)
        # STOPP väntar inte på något svar.
        Thread.objects.filter(lead=reply).update(kind=Thread.Kind.STOP)
        self.assertNotIn("svar på ditt sms", self.html())

    def test_what_waits_for_adx(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(status=Campaign.STATUS_IN_REVIEW)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_REQUESTED_NEW
        )
        html = self.html()
        self.assertIn("<b>Kampanjen Rörjour Nacka granskas av ADX</b>", html)
        self.assertIn("<b>ADX kopplar ditt Google Ads-konto</b>", html)
        self.assertIn("Du behöver inte göra något.", html)
        self.assertIn("fl-todo__item--adx", html)

    def test_nothing_to_do(self):
        Lead.objects.filter(account=self.account).update(status=Lead.STATUS_CONTACTED)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            scan_status=FlamingoAccount.SCAN_DONE,
            google_status=FlamingoAccount.GOOGLE_BILLING_OK,
        )
        html = self.html()
        self.assertIn("Inget väntar på dig just nu.", html)
