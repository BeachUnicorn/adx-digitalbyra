"""Kontakter S1: verktygets vyer (README I.1, I.7, I.9, H.1, H.3, H.4, J S1).

Varje adress är 404 för ett annat kontos rader och när utskick är av;
främmande id:n i ett formulär ger 400; byrån i kundvyn gör det kunden gör
och loggas; menyn visar Kontakter bara när utskick är på; exporten är POST,
loggas och har ett tak; "Markera alla som matchar"; tidslinjen visar bara
kontots förfrågningar; biträdesavtalet stoppar nya kontakter; GDPR-export och
borttagning; listor, taggar, extrafält, inställningar och rensning."""

import json
import re
from pathlib import Path

from django.conf import settings
from django.core import mail
from django.test import TestCase
from django.urls import URLPattern, reverse

from apps.flamingo.models import Lead

from . import app_urls, consent, contacts, keys
from .models import (
    ConsentLog,
    Contact,
    ContactList,
    DpaAcceptance,
    DpaVersion,
    Event,
    ExportLog,
    FieldDef,
    ImportJob,
    ListMembership,
    Segment,
    Suppression,
    Tag,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from .testing import PHONE_ANNA, PHONE_BO, PHONE_CILLA, UtskickFixture, make_contact

TEMPLATES = Path(settings.BASE_DIR) / "templates" / "flamingo" / "app" / "kontakter"
#: Mallarna Kontakter-agenten äger (importens mallar har en egen ägare).
OWN_TEMPLATES = [p for p in TEMPLATES.glob("*.html") if not p.name.startswith("import_")]
#: Vyerna i den här delen som ska svara 200 för kunden.
OWN_GET_ROUTES = (
    "app_contacts",
    "app_contact_new",
    "app_contacts_export",
    "app_contacts_prune",
    "app_contact",
    "app_contact_edit",
    "app_contact_consent",
    "app_contact_delete",
    "app_lists",
    "app_list",
    "app_fields",
    "app_contacts_settings",
    "app_dpa",
)


def _routes():
    """(namn, tar pk) för varje adress i app_urls."""
    for pattern in app_urls.urlpatterns:
        if isinstance(pattern, URLPattern):
            yield pattern.name, "<int:pk>" in str(pattern.pattern)


def _model_for(name):
    # S2: utskicken och svaren i Inkorgen (app_views/utskick.py, inbox_reply.py).
    # S3: e-postredigeraren (app_views/brev.py) tar utskickets pk.
    if name.startswith(("app_utskick", "app_brev")):
        return Utskick
    # S4: segmenten och de namngivna länkarna.
    if name.startswith("app_segment"):
        return Segment
    if name.startswith("app_link"):
        return TrackedLink
    if name.startswith("app_lead"):
        return Lead
    if "import" in name:
        return ImportJob
    if name == "app_list":
        return ContactList
    return Contact


class ViewsFixture(UtskickFixture):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)
        self.kontakt = make_contact(
            self.account,
            first_name="Anna",
            last_name="Lind",
            phone=PHONE_ANNA,
            email="anna@lind.example",
        )
        self.lista = ContactList.objects.create(account=self.account, name="Däckhotell")
        self.tag = Tag.objects.create(account=self.account, name="Bromma")
        self.import_job = ImportJob.objects.create(
            account=self.account, original_name="kunder.csv", kind="csv"
        )
        # Det andra kontot: ingenting av det här får synas eller röras.
        self.foreign = make_contact(
            self.other_account, first_name="Hemlig", last_name="Person", phone=PHONE_ANNA
        )
        self.foreign_list = ContactList.objects.create(account=self.other_account, name="Hemlig")
        self.foreign_tag = Tag.objects.create(account=self.other_account, name="Hemlig")
        self.foreign_job = ImportJob.objects.create(
            account=self.other_account, original_name="hemlig.csv", kind="csv"
        )
        self.foreign_field = FieldDef.objects.create(
            account=self.other_account, key="hemligt", label="Hemligt"
        )
        # S2: ett utskick och en svarsförfrågan per konto.
        self.utskick = Utskick.objects.create(account=self.account, name="Höstservice")
        self.foreign_utskick = Utskick.objects.create(account=self.other_account, name="Hemligt")
        self.reply_lead = Lead.objects.create(account=self.account, source=Lead.SOURCE_REPLY)
        self.foreign_lead = Lead.objects.create(
            account=self.other_account, source=Lead.SOURCE_REPLY
        )
        # S4: ett segment och en namngiven länk per konto.
        self.segment = Segment.objects.create(account=self.account, name="Service i höst")
        self.foreign_segment = Segment.objects.create(account=self.other_account, name="Hemligt")
        self.named_link = TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.EXTERNAL,
            slug="vinter",
            destination="https://exempelror.example/vinter",
        )
        self.foreign_named_link = TrackedLink.objects.create(
            account=self.other_account,
            kind=TrackedLink.Kind.EXTERNAL,
            slug="hemlig",
            destination="https://annanfirma.example/",
        )

    def own_pk(self, name):
        rows = {
            Contact: self.kontakt,
            ContactList: self.lista,
            ImportJob: self.import_job,
            Utskick: self.utskick,
            Lead: self.reply_lead,
            Segment: self.segment,
            TrackedLink: self.named_link,
        }
        return rows[_model_for(name)].pk

    def foreign_pk(self, name):
        rows = {
            Contact: self.foreign,
            ContactList: self.foreign_list,
            ImportJob: self.foreign_job,
            Utskick: self.foreign_utskick,
            Lead: self.foreign_lead,
            Segment: self.foreign_segment,
            TrackedLink: self.foreign_named_link,
        }
        return rows[_model_for(name)].pk

    def url(self, name, pk=None):
        if name == "app_utskick_step":
            return reverse(f"flamingo:{name}", kwargs={"pk": pk, "step": "granska"})
        if name == "app_utskick_reply_confirm":
            # S3: länken för den egna svarsadressen bär en token, inget pk.
            return reverse(f"flamingo:{name}", args=["abc.def"])
        if name in ("app_link_qr", "app_signup_qr"):
            # S4: QR-kodens format står i adressen.
            args = [pk, "svg"] if pk is not None else ["svg"]
            return reverse(f"flamingo:{name}", args=args)
        return reverse(f"flamingo:{name}", args=[pk] if pk is not None else [])


# ---------------------------------------------------------------------------
# Behörighet och konton (H.1)
# ---------------------------------------------------------------------------


class TenancyTests(ViewsFixture, TestCase):
    def assert_hidden(self, method, name):
        """404 för ett annat kontos rad. 405 räknas bara när adressen svarar
        405 för kontots egen rad också (fel metod, inget som läcker)."""
        call = getattr(self.client, method)
        foreign = call(self.url(name, self.foreign_pk(name)), {"action": "delete"}).status_code
        if foreign == 405:
            own = call(self.url(name, self.own_pk(name)), {"action": "x"}).status_code
            self.assertEqual(own, 405, f"{method} {name}")
        else:
            self.assertEqual(foreign, 404, f"{method} {name}")

    def test_every_route_is_404_for_another_accounts_row(self):
        for name, has_pk in _routes():
            if not has_pk:
                continue
            with self.subTest(route=name):
                self.assert_hidden("get", name)
                self.assert_hidden("post", name)
        self.assertTrue(Contact.objects.filter(pk=self.foreign.pk).exists())
        self.assertTrue(ContactList.objects.filter(pk=self.foreign_list.pk).exists())

    def test_every_route_is_404_when_utskick_is_off(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        for client in (self.client, self.client_for(self.staff)):
            for name, has_pk in _routes():
                url = self.url(name, self.own_pk(name) if has_pk else None)
                with self.subTest(route=name, staff=client is not self.client):
                    self.assertEqual(client.get(url).status_code, 404)
                    self.assertEqual(client.post(url, {"action": "delete"}).status_code, 404)
        self.assertTrue(Contact.objects.filter(pk=self.kontakt.pk).exists())

    def test_own_routes_answer_for_the_customer_and_for_staff_in_view_as(self):
        for client in (self.client, self.client_for(self.staff)):
            for name in OWN_GET_ROUTES:
                has_pk = dict(_routes())[name]
                url = self.url(name, self.own_pk(name) if has_pk else None)
                with self.subTest(route=name):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context["app_active"], "contacts")
                    self.assertNotContains(response, "Hemlig")

    def test_the_menu_shows_kontakter_only_when_utskick_is_on(self):
        link = f'href="{reverse("flamingo:app_contacts")}"'
        html = self.client.get(reverse("flamingo:app")).content.decode()
        self.assertIn(link, html)
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        html = self.client.get(reverse("flamingo:app")).content.decode()
        self.assertNotIn(link, html)
        self.assertNotIn(">Kontakter<", html)

    def test_the_list_never_shows_another_accounts_contacts(self):
        response = self.client.get(self.url("app_contacts"))
        self.assertContains(response, "Anna Lind")
        self.assertNotContains(response, "Hemlig")
        # Ett främmande list-id i adressraden hoppas över, och läcker inget.
        response = self.client.get(self.url("app_contacts") + f"?lista={self.foreign_list.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Hemlig")


class ForeignIdTests(ViewsFixture, TestCase):
    """Varje id ur en förfrågans kropp går genom owned_ids: 400 och ingen
    ändring när ett enda är främmande."""

    def bulk(self, **data):
        return self.client.post(self.url("app_contacts_bulk"), data)

    def test_bulk_with_a_foreign_contact_is_400(self):
        response = self.bulk(
            action="tag_add", tagg_id=self.tag.pk, ids=[self.kontakt.pk, self.foreign.pk]
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.tag.contacts.exists())
        self.assertFalse(self.foreign.tags.exists())

    def test_bulk_with_a_foreign_target_list_or_tag_is_400(self):
        response = self.bulk(
            action="list_add", lista_id=self.foreign_list.pk, ids=[self.kontakt.pk]
        )
        self.assertEqual(response.status_code, 400)
        response = self.bulk(action="tag_add", tagg_id=self.foreign_tag.pk, ids=[self.kontakt.pk])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ListMembership.objects.exists())
        self.assertFalse(self.foreign_tag.contacts.exists())

    def test_select_all_with_a_foreign_filter_is_400(self):
        response = self.bulk(
            action="tag_add", tagg_id=self.tag.pk, alla="1", lista=self.foreign_list.pk
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.tag.contacts.exists())

    def test_garbage_and_too_many_ids_are_400(self):
        self.assertEqual(
            self.bulk(action="tag_add", tagg_id=self.tag.pk, ids=["1 OR 1=1"]).status_code, 400
        )
        many = [str(self.kontakt.pk)] + [str(n) for n in range(10**6, 10**6 + 200)]
        self.assertEqual(
            self.bulk(action="tag_add", tagg_id=self.tag.pk, ids=many).status_code, 400
        )

    def test_card_list_and_tag_ids_are_checked(self):
        url = self.url("app_contact", self.kontakt.pk)
        self.assertEqual(
            self.client.post(
                url, {"action": "list_add", "lista_id": self.foreign_list.pk}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                url, {"action": "tag_remove", "tagg_id": self.foreign_tag.pk}
            ).status_code,
            400,
        )
        self.assertFalse(ListMembership.objects.exists())

    def test_list_page_tag_and_field_ids_are_checked(self):
        url = self.url("app_list", self.lista.pk)
        response = self.client.post(url, {"action": "remove", "ids": [self.foreign.pk]})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            self.url("app_lists"), {"action": "tag_delete", "tagg_id": self.foreign_tag.pk}
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Tag.objects.filter(pk=self.foreign_tag.pk).exists())
        response = self.client.post(
            self.url("app_fields"), {"action": "delete", "falt": self.foreign_field.pk}
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(FieldDef.objects.filter(pk=self.foreign_field.pk).exists())

    def test_export_with_a_foreign_id_is_400(self):
        response = self.client.post(
            self.url("app_contacts_export"), {"bekrafta": "1", "ids": [self.foreign.pk]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ExportLog.objects.exists())


# ---------------------------------------------------------------------------
# Listan, sökningen och massändringen
# ---------------------------------------------------------------------------


class ListAndBulkTests(ViewsFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.bo = make_contact(self.account, first_name="Bo", last_name="Berg", phone=PHONE_BO)
        self.cilla = make_contact(
            self.account, first_name="Cilla", last_name="Bergström", phone=PHONE_CILLA
        )

    def test_header_counts_and_rows(self):
        consent.set_status(self.bo, "sms", "existing", source="manual", evidence="kassan")
        response = self.client.get(self.url("app_contacts"))
        self.assertContains(response, "3 kontakter · 1 kan få sms · 0 kan få e-post")
        self.assertContains(response, "Sms: befintlig kund")
        self.assertContains(response, "Sms: inget samtycke")
        self.assertContains(response, "070-174 06 02")
        self.assertContains(response, 'data-label="Samtycke"')

    def test_search_and_filters(self):
        response = self.client.get(self.url("app_contacts") + "?q=berg")
        self.assertContains(response, "Bo Berg")
        self.assertContains(response, "Cilla Bergström")
        self.assertNotContains(response, "Anna Lind")
        consent.set_status(self.bo, "sms", "yes", source="manual", evidence="kassan")
        response = self.client.get(self.url("app_contacts") + "?samtycke=ja&kanal=sms")
        self.assertContains(response, "Bo Berg")
        self.assertNotContains(response, "Cilla Bergström")
        contacts.add_to_list(self.lista, [self.cilla])
        response = self.client.get(self.url("app_contacts") + f"?lista={self.lista.pk}")
        self.assertContains(response, "Cilla Bergström")
        self.assertNotContains(response, "Bo Berg")

    def test_bulk_tags_only_the_checked(self):
        response = self.client.post(
            self.url("app_contacts_bulk"),
            {"action": "list_add", "lista_id": "ny", "ny_lista": "Höst", "ids": [self.bo.pk]},
        )
        self.assertEqual(response.status_code, 302)
        lista = ContactList.objects.get(account=self.account, name="Höst")
        self.assertEqual(list(lista.memberships.values_list("contact_id", flat=True)), [self.bo.pk])

    def test_select_all_that_match_posts_the_filter_instead_of_ids(self):
        response = self.client.post(
            self.url("app_contacts_bulk"),
            {"action": "tag_add", "tagg_id": self.tag.pk, "alla": "1", "q": "berg"},
        )
        self.assertRedirects(response, self.url("app_contacts") + "?q=berg")
        tagged = set(self.tag.contacts.values_list("pk", flat=True))
        self.assertEqual(tagged, {self.bo.pk, self.cilla.pk})
        self.assertFalse(self.foreign.tags.exists())
        # Utan filter gäller "alla" hela registret, men bara kontots eget.
        self.client.post(
            self.url("app_contacts_bulk"),
            {"action": "list_add", "lista_id": self.lista.pk, "alla": "1"},
        )
        self.assertEqual(self.lista.memberships.count(), 3)
        self.assertFalse(ListMembership.objects.filter(contact=self.foreign).exists())

    def test_remove_tag_and_missing_choices(self):
        contacts.add_tag(self.tag, [self.bo, self.cilla])
        self.client.post(
            self.url("app_contacts_bulk"),
            {"action": "tag_remove", "tagg_id": self.tag.pk, "ids": [self.bo.pk]},
        )
        self.assertEqual(list(self.tag.contacts.values_list("pk", flat=True)), [self.cilla.pk])
        response = self.client.post(
            self.url("app_contacts_bulk"), {"action": "tag_add", "ids": [self.bo.pk]}, follow=True
        )
        self.assertContains(response, "Välj en tagg.")
        response = self.client.post(
            self.url("app_contacts_bulk"),
            {"action": "tag_add", "tagg_id": self.tag.pk},
            follow=True,
        )
        self.assertContains(response, "Markera minst en kontakt.")

    def test_bulk_delete_asks_first_then_deletes_and_suppresses(self):
        data = {"action": "delete", "ids": [self.bo.pk, self.cilla.pk]}
        response = self.client.post(self.url("app_contacts_bulk"), data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "flamingo/app/kontakter/delete.html")
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 3)
        data.update(bekrafta="1", sparr="1")
        response = self.client.post(self.url("app_contacts_bulk"), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Contact.objects.filter(account=self.account)), [self.kontakt])
        hashes = set(
            Suppression.objects.filter(account=self.account, reason="erasure").values_list(
                "value_hash", flat=True
            )
        )
        self.assertEqual(
            hashes, {keys.value_hash("sms", PHONE_BO), keys.value_hash("sms", PHONE_CILLA)}
        )

    def test_bulk_export_asks_and_then_exports_the_checked(self):
        data = {"action": "export", "ids": [self.bo.pk, self.cilla.pk]}
        response = self.client.post(self.url("app_contacts_bulk"), data)
        self.assertTemplateUsed(response, "flamingo/app/kontakter/export.html")
        self.assertContains(response, f'name="ids" value="{self.bo.pk}"')
        self.assertContains(response, "Exportera 2 kontakter")
        self.assertFalse(ExportLog.objects.exists())
        response = self.client.post(
            self.url("app_contacts_export"), {"bekrafta": "1", "ids": [self.bo.pk, self.cilla.pk]}
        )
        text = response.content.decode("utf-8")
        self.assertIn("Bo,Berg", text)
        self.assertNotIn("Anna,Lind", text)
        self.assertEqual(ExportLog.objects.get().rows, 2)

    def test_pages_hold_fifty(self):
        for n in range(55):
            Contact.objects.create(account=self.account, first_name=f"Rad{n}", source="manual")
        response = self.client.get(self.url("app_contacts"))
        self.assertEqual(len(response.context["kontakter"]), 50)
        self.assertContains(response, "Markera alla 58 som matchar")
        response = self.client.get(self.url("app_contacts") + "?sida=2")
        self.assertEqual(len(response.context["kontakter"]), 8)

    def test_empty_states(self):
        Contact.objects.filter(account=self.account).delete()
        self.assertContains(self.client.get(self.url("app_contacts")), "Inga kontakter än")
        DpaAcceptance.objects.filter(account=self.account).delete()
        self.assertContains(
            self.client.get(self.url("app_contacts")),
            "Godkänn biträdesavtalet för att lägga till kontakter.",
        )


# ---------------------------------------------------------------------------
# Exporten (H.3)
# ---------------------------------------------------------------------------


class ExportTests(ViewsFixture, TestCase):
    def export(self, **data):
        data.setdefault("bekrafta", "1")
        data.setdefault("alla", "1")
        return self.client.post(self.url("app_contacts_export"), data)

    def test_get_only_asks(self):
        response = self.client.get(self.url("app_contacts_export"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertContains(response, "Exportera 1 kontakt")
        self.assertFalse(ExportLog.objects.exists())

    def test_post_gives_a_safe_csv_and_is_logged(self):
        make_contact(self.account, first_name="=HYPERLINK(1)", phone=PHONE_BO)
        response = self.export()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        text = response.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿Förnamn,Efternamn"))
        self.assertIn("Anna,Lind", text)
        self.assertIn("'=HYPERLINK(1)", text)
        self.assertNotIn("Hemlig", text)
        log = ExportLog.objects.get()
        self.assertEqual(
            (log.kind, log.rows, log.user, log.as_staff), ("contacts", 2, self.anna, False)
        )

    def test_at_most_ten_exports_a_day(self):
        for _ in range(10):
            self.assertEqual(self.export()["Content-Type"], "text/csv; charset=utf-8")
        response = self.export()
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertContains(response, "gränsen")
        self.assertEqual(ExportLog.objects.count(), 10)

    def test_staff_export_is_logged_as_adx(self):
        self.client_for(self.staff).post(
            self.url("app_contacts_export"), {"bekrafta": "1", "alla": "1"}
        )
        log = ExportLog.objects.get()
        self.assertTrue(log.as_staff)
        response = self.client.get(self.url("app_contacts_settings"))
        self.assertContains(response, "Senast exporterad av ADX")


# ---------------------------------------------------------------------------
# Lägg till, redigera, samtycket och byrån i kundvyn
# ---------------------------------------------------------------------------


class ContactFormTests(ViewsFixture, TestCase):
    def new(self, client=None, **data):
        return (client or self.client).post(self.url("app_contact_new"), data)

    def test_add_a_contact_with_consent_and_evidence(self):
        response = self.new(
            first_name="Bo",
            last_name="Berg",
            phone="070-174 06 02",
            consent_sms="existing",
            evidence="kund sedan 2023",
        )
        kontakt = Contact.objects.get(account=self.account, phone=PHONE_BO)
        self.assertRedirects(response, self.url("app_contact", kontakt.pk))
        self.assertEqual(kontakt.source, "manual")
        row = kontakt.consents.get(channel="sms")
        self.assertEqual((row.status, row.evidence), ("existing", "kund sedan 2023"))
        self.assertFalse(ConsentLog.objects.get(contact=kontakt, new_status="existing").by_staff)

    def test_consent_without_evidence_or_address_is_refused(self):
        response = self.new(first_name="Bo", phone="070-174 06 02", consent_sms="yes")
        self.assertContains(response, consent.REFUSAL_TEXTS["evidence"])
        response = self.new(first_name="Bo", consent_email="yes", evidence="kassan")
        self.assertContains(response, "Fyll i en e-postadress")
        self.assertFalse(Contact.objects.filter(first_name="Bo").exists())

    def test_duplicates_and_personnummer_are_form_errors(self):
        response = self.new(first_name="Kopia", phone=PHONE_ANNA)
        self.assertContains(response, contacts.DUPLICATE_PHONE)
        FieldDef.objects.create(account=self.account, key="anteckning", label="Anteckning")
        response = self.new(first_name="Ny", falt_anteckning="19900101-1239")
        self.assertContains(response, "Personnummer sparas inte i Kontakter.")
        self.assertFalse(Contact.objects.filter(first_name__in=["Kopia", "Ny"]).exists())

    def test_edit_a_new_email_resets_its_consent(self):
        consent.set_status(self.kontakt, "email", "yes", source="manual", evidence="mässan")
        response = self.client.post(
            self.url("app_contact_edit", self.kontakt.pk),
            {
                "kind": "person",
                "first_name": "Anna",
                "last_name": "Lindh",
                "phone": "070-174 06 01",
                "email": "ny@lind.example",
            },
        )
        self.assertRedirects(response, self.url("app_contact", self.kontakt.pk))
        self.kontakt.refresh_from_db()
        self.assertEqual((self.kontakt.last_name, self.kontakt.email), ("Lindh", "ny@lind.example"))
        self.assertEqual(self.kontakt.consents.get(channel="email").status, "missing")
        self.assertEqual(self.kontakt.consents.get(channel="sms").status, "missing")
        self.assertContains(
            self.client.get(self.url("app_contact", self.kontakt.pk)), "Ny e-postadress"
        )

    def test_staff_in_view_as_acts_for_real_and_is_logged(self):
        staff = self.client_for(self.staff)
        self.new(
            staff, first_name="Bo", phone="070-174 06 02", consent_sms="yes", evidence="mässan"
        )
        kontakt = Contact.objects.get(account=self.account, phone=PHONE_BO)
        log = ConsentLog.objects.get(contact=kontakt, new_status="yes")
        self.assertTrue(log.by_staff)
        self.assertEqual(log.by_label, "ADX (byra)")
        staff.post(
            self.url("app_contact_consent", kontakt.pk), {"channel": "sms", "status": "declined"}
        )
        log = ConsentLog.objects.get(contact=kontakt, new_status="declined")
        self.assertTrue(log.by_staff)


class ConsentViewTests(ViewsFixture, TestCase):
    def change(self, **data):
        return self.client.post(self.url("app_contact_consent", self.kontakt.pk), data)

    def test_get_draws_the_card_with_the_box_open(self):
        response = self.client.get(
            self.url("app_contact_consent", self.kontakt.pk) + "?kanal=email"
        )
        self.assertTemplateUsed(response, "flamingo/app/kontakter/detail.html")
        self.assertContains(response, 'id="samtycke"')
        self.assertContains(response, 'name="channel" value="email" checked')

    def test_yes_needs_evidence_and_is_proved_on_the_card(self):
        response = self.change(channel="sms", status="yes")
        self.assertContains(response, consent.REFUSAL_TEXTS["evidence"])
        response = self.change(channel="sms", status="yes", evidence="i kassan, mars 2024")
        self.assertRedirects(response, self.url("app_contact", self.kontakt.pk))
        html = self.client.get(self.url("app_contact", self.kontakt.pk)).content.decode()
        self.assertIn("Sms: ja", html)
        self.assertIn("Visa beviset", html)
        self.assertIn("i kassan, mars 2024", html)
        self.assertIn("Anna Lindqvist", html)

    def test_unsubscribe_suppresses_and_only_the_person_can_undo_it(self):
        self.change(channel="email", status="unsubscribed")
        self.assertTrue(
            Suppression.objects.filter(
                account=self.account, value_hash=keys.value_hash("email", "anna@lind.example")
            ).exists()
        )
        response = self.change(channel="email", status="yes", evidence="hon ringde")
        self.assertContains(response, consent.REFUSAL_TEXTS["locked"])
        self.assertEqual(self.kontakt.consents.get(channel="email").status, "unsubscribed")


# ---------------------------------------------------------------------------
# Kortet och tidslinjen
# ---------------------------------------------------------------------------


class CardTests(ViewsFixture, TestCase):
    def test_lists_and_tags_on_the_card(self):
        url = self.url("app_contact", self.kontakt.pk)
        self.client.post(url, {"action": "list_add", "lista_id": self.lista.pk})
        self.client.post(url, {"action": "tag_add", "tagg_id": "ny", "ny_tagg": "Företagskund"})
        self.assertTrue(self.lista.memberships.filter(contact=self.kontakt).exists())
        self.assertEqual(list(self.kontakt.tags.values_list("name", flat=True)), ["Företagskund"])
        html = self.client.get(url).content.decode()
        self.assertIn("Däckhotell", html)
        self.assertIn("Företagskund", html)
        self.client.post(url, {"action": "list_remove", "lista_id": self.lista.pk})
        self.assertFalse(self.lista.memberships.exists())

    def test_header_line_fields_and_summary(self):
        FieldDef.objects.create(account=self.account, key="regnr", label="Regnr", show_in_list=True)
        contacts.update(self.kontakt, {"fields": {"regnr": "ABC 123"}})
        response = self.client.get(self.url("app_contact", self.kontakt.pk))
        self.assertContains(response, "Privatperson · kontakt sedan")
        self.assertContains(response, "källa: manuellt")
        self.assertContains(response, "ABC 123")
        self.assertContains(response, "Inga förfrågningar än.")
        self.assertContains(response, "Lades till i Kontakter")
        html = self.client.get(self.url("app_contacts")).content.decode()
        self.assertIn("Privatperson · Regnr ABC 123", html)

    def test_the_timeline_shows_only_this_accounts_leads(self):
        own = Lead.objects.create(account=self.account, name="Anna Lind", contact=self.kontakt)
        # En förfrågan i ett annat konto som (felaktigt) pekar på kontakten.
        Lead.objects.create(account=self.other_account, name="Hemlig Fråga", contact=self.kontakt)
        contacts.record_event(self.kontakt, Event.IMPORTED, {"import": 12})
        response = self.client.get(self.url("app_contact", self.kontakt.pk))
        self.assertContains(response, reverse("flamingo:app_lead", args=[own.pk]))
        self.assertContains(response, "Öppna i Inkorgen")
        self.assertContains(response, "Importerad")
        self.assertContains(response, "1 förfrågan")
        self.assertNotContains(response, "Hemlig")


# ---------------------------------------------------------------------------
# GDPR per person (H.4)
# ---------------------------------------------------------------------------


class GdprTests(ViewsFixture, TestCase):
    def test_export_is_post_only_json_and_logged(self):
        url = self.url("app_contact_export", self.kontakt.pk)
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        data = json.loads(response.content.decode("utf-8"))
        self.assertEqual(data["kontakt"]["förnamn"], "Anna")
        log = ExportLog.objects.get()
        self.assertEqual((log.kind, log.rows), ("contact", 1))

    def test_delete_asks_then_removes_leads_suppresses_and_keeps_the_proof(self):
        consent.set_status(self.kontakt, "sms", "yes", source="manual", evidence="kassan")
        lead = Lead.objects.create(account=self.account, name="Anna Lind", contact=self.kontakt)
        url = self.url("app_contact_delete", self.kontakt.pk)
        response = self.client.get(url)
        self.assertContains(response, "Ta också bort förfrågningar från personen (1)")
        self.assertContains(response, 'name="sparr" value="1" checked')
        response = self.client.post(url, {"forfragningar": "1", "sparr": "1"})
        self.assertRedirects(response, self.url("app_contacts"))
        self.assertFalse(Contact.objects.filter(pk=self.kontakt.pk).exists())
        self.assertFalse(Lead.objects.filter(pk=lead.pk).exists())
        reasons = set(
            Suppression.objects.filter(account=self.account).values_list("channel", "reason")
        )
        self.assertEqual(reasons, {("sms", "erasure"), ("email", "erasure")})
        logs = ConsentLog.objects.filter(account=self.account, new_status="yes")
        self.assertEqual(logs.count(), 1)
        self.assertIsNone(logs.get().contact_id)
        self.assertEqual(logs.get().evidence, "")

    def test_unticked_boxes_keep_leads_and_add_no_suppression(self):
        lead = Lead.objects.create(account=self.account, name="Anna Lind", contact=self.kontakt)
        self.client.post(self.url("app_contact_delete", self.kontakt.pk), {})
        self.assertTrue(Lead.objects.filter(pk=lead.pk).exists())
        self.assertFalse(Suppression.objects.filter(account=self.account).exists())


# ---------------------------------------------------------------------------
# Biträdesavtalet (D6, I.4)
# ---------------------------------------------------------------------------


class DpaTests(ViewsFixture, TestCase):
    def setUp(self):
        super().setUp()
        DpaAcceptance.objects.filter(account=self.account).delete()

    def test_no_new_contact_before_the_dpa_is_accepted(self):
        response = self.client.get(self.url("app_contact_new"))
        self.assertRedirects(response, self.url("app_dpa") + "?nasta=ny")
        self.client.post(self.url("app_contact_new"), {"first_name": "Bo"})
        self.assertFalse(Contact.objects.filter(first_name="Bo").exists())
        response = self.client.get(self.url("app_contacts"))
        self.assertContains(response, "Lägg till kontakter: godkänn biträdesavtalet först.")

    def test_the_customer_accepts_and_comes_back(self):
        response = self.client.get(self.url("app_dpa") + "?nasta=ny")
        self.assertContains(response, "Biträdesavtal")
        response = self.client.post(self.url("app_dpa"), {"version": self.dpa.pk, "nasta": "ny"})
        self.assertContains(response, "Kryssa i att du har läst avtalet")
        response = self.client.post(
            self.url("app_dpa"), {"version": self.dpa.pk, "accept": "1", "nasta": "ny"}
        )
        self.assertRedirects(response, self.url("app_contact_new"))
        acceptance = DpaAcceptance.objects.get(account=self.account)
        self.assertEqual((acceptance.accepted_by, acceptance.accepted_as_staff), (self.anna, False))
        self.assertEqual(self.client.get(self.url("app_contact_new")).status_code, 200)
        self.assertContains(
            self.client.get(self.url("app_contacts_settings")), "Godkänt av Anna Lindqvist"
        )

    def test_staff_must_say_who_approved_and_how(self):
        staff = self.client_for(self.staff)
        response = staff.post(self.url("app_dpa"), {"version": self.dpa.pk, "accept": "1"})
        self.assertContains(response, "Skriv vem hos kunden som godkände avtalet")
        self.assertFalse(DpaAcceptance.objects.filter(account=self.account).exists())
        staff.post(
            self.url("app_dpa"),
            {
                "version": self.dpa.pk,
                "accept": "1",
                "staff_statement": "Anna Lindqvist, i mejl 2 okt",
            },
        )
        acceptance = DpaAcceptance.objects.get(account=self.account)
        self.assertTrue(acceptance.accepted_as_staff)
        self.assertEqual(acceptance.staff_statement, "Anna Lindqvist, i mejl 2 okt")

    def test_a_new_version_needs_a_new_acceptance_and_a_stale_form_is_refused(self):
        DpaAcceptance.objects.create(account=self.account, version=self.dpa, accepted_by=self.anna)
        self.dpa.is_current = False
        self.dpa.save()
        new = DpaVersion.objects.create(
            version="2026-11", text="Ny text", sha256="1" * 64, is_current=True
        )
        self.assertEqual(self.client.get(self.url("app_contact_new")).status_code, 302)
        response = self.client.post(self.url("app_dpa"), {"version": self.dpa.pk, "accept": "1"})
        self.assertContains(response, "Avtalet har ändrats medan du läste.")
        self.client.post(self.url("app_dpa"), {"version": new.pk, "accept": "1"})
        self.assertEqual(self.client.get(self.url("app_contact_new")).status_code, 200)

    def test_without_a_current_version_the_agency_is_alerted_once(self):
        DpaVersion.objects.update(is_current=False)
        mail.outbox.clear()
        for _ in range(2):
            response = self.client.get(self.url("app_dpa"))
            self.assertContains(response, "Avtalssidan saknas. Kontakta ADX.")
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("anna@", mail.outbox[0].body)
        self.assertNotIn("anna@exempelror.example", mail.outbox[0].to)

    def test_the_demo_needs_no_dpa_and_never_alerts(self):
        from apps.flamingo.models import FlamingoAccount

        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        DpaVersion.objects.update(is_current=False)
        mail.outbox.clear()
        response = self.client.get(self.url("app_dpa"))
        self.assertContains(response, "Demokontot behöver inget godkännande.")
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.client.get(self.url("app_contact_new")).status_code, 200)


# ---------------------------------------------------------------------------
# Listor, taggar, extrafält, inställningar och rensning
# ---------------------------------------------------------------------------


class ListsAndTagsTests(ViewsFixture, TestCase):
    def test_create_rename_and_delete_keep_the_contacts(self):
        response = self.client.post(self.url("app_lists"), {"action": "list_new", "namn": "Vinter"})
        lista = ContactList.objects.get(account=self.account, name="Vinter")
        self.assertRedirects(response, self.url("app_list", lista.pk))
        response = self.client.post(
            self.url("app_lists"), {"action": "list_new", "namn": "Vinter"}, follow=True
        )
        self.assertContains(response, "Det finns redan en lista med det namnet.")
        contacts.add_to_list(lista, [self.kontakt])
        self.client.post(self.url("app_list", lista.pk), {"action": "rename", "namn": "Vinterdäck"})
        lista.refresh_from_db()
        self.assertEqual(lista.name, "Vinterdäck")
        self.assertContains(self.client.get(self.url("app_list", lista.pk)), "Anna Lind")
        self.client.post(
            self.url("app_list", lista.pk), {"action": "remove", "ids": [self.kontakt.pk]}
        )
        self.assertFalse(lista.memberships.exists())
        self.client.post(self.url("app_list", lista.pk), {"action": "delete"})
        self.assertFalse(ContactList.objects.filter(pk=lista.pk).exists())
        self.assertTrue(Contact.objects.filter(pk=self.kontakt.pk).exists())

    def test_tags(self):
        self.client.post(self.url("app_lists"), {"action": "tag_new", "namn": "Fleet"})
        tag = Tag.objects.get(account=self.account, name="Fleet")
        contacts.add_tag(tag, [self.kontakt])
        self.client.post(
            self.url("app_lists"), {"action": "tag_rename", "tagg_id": tag.pk, "namn": "Vagnpark"}
        )
        tag.refresh_from_db()
        self.assertEqual(tag.name, "Vagnpark")
        self.client.post(self.url("app_lists"), {"action": "tag_delete", "tagg_id": tag.pk})
        self.assertFalse(Tag.objects.filter(pk=tag.pk).exists())
        self.assertTrue(Contact.objects.filter(pk=self.kontakt.pk).exists())


class FieldTests(ViewsFixture, TestCase):
    def post(self, **data):
        return self.client.post(self.url("app_fields"), data)

    def test_new_fields_get_a_stable_key_and_one_shows_in_the_list(self):
        self.post(action="new", label="Senaste besök", kind="date", show_in_list="1")
        first = FieldDef.objects.get(account=self.account, label="Senaste besök")
        self.assertEqual(
            (first.key, first.kind, first.show_in_list), ("senaste-besok", "date", True)
        )
        self.post(action="new", label="Regnr", kind="text", show_in_list="1")
        first.refresh_from_db()
        self.assertFalse(first.show_in_list)
        self.assertTrue(FieldDef.objects.get(account=self.account, key="regnr").show_in_list)
        self.post(action="edit", falt=first.pk, label="Senaste service")
        first.refresh_from_db()
        self.assertEqual((first.key, first.label), ("senaste-besok", "Senaste service"))

    def test_choice_needs_two_and_a_used_choice_stays(self):
        response = self.post(action="new", label="Hylla", kind="choice", choices="A")
        self.assertContains(response, "Skriv minst två val")
        self.post(action="new", label="Hylla", kind="choice", choices="A\nB")
        field = FieldDef.objects.get(account=self.account, key="hylla")
        contacts.update(self.kontakt, {"fields": {"hylla": "B"}})
        response = self.post(action="edit", falt=field.pk, label="Hylla", choices="A\nC")
        self.assertContains(response, "Ett val du tog bort används av 1 kontakt.")
        field.refresh_from_db()
        self.assertEqual(field.choices, ["A", "B"])

    def test_at_most_thirty_and_delete_removes_the_values(self):
        for n in range(30):
            FieldDef.objects.create(account=self.account, key=f"f{n}", label=f"Fält {n}", order=n)
        response = self.post(action="new", label="Ett till", kind="text")
        self.assertRedirects(response, self.url("app_fields"), fetch_redirect_response=False)
        self.assertEqual(FieldDef.objects.filter(account=self.account).count(), 30)
        contacts.update(self.kontakt, {"fields": {"f1": "ABC123"}})
        self.post(action="delete", falt=FieldDef.objects.get(account=self.account, key="f1").pk)
        self.kontakt.refresh_from_db()
        self.assertNotIn("f1", self.kontakt.fields)
        self.assertNotIn("abc123", self.kontakt.search_text)
        self.assertContains(self.client.get(self.url("app_fields")), "Spara inte personnummer")


class SettingsTests(ViewsFixture, TestCase):
    def data(self, **changes):
        data = {
            "consent_text_sms": "Ja, jag vill få erbjudanden från Exempelrör via sms.",
            "consent_text_email": "Ja, jag vill få nyheter från Exempelrör via e-post.",
            "lp_consent": "1",
            "privacy_url": "",
            "pref_email_note": "Ungefär en gång i månaden.",
            "unsubscribe_text": "",
        }
        data.update(changes)
        return data

    def test_the_customer_edits_the_s1_rows_only(self):
        response = self.client.post(
            self.url("app_contacts_settings"),
            self.data(display_name="Något annat", public_slug="annat", is_enabled=""),
        )
        self.assertRedirects(response, self.url("app_contacts_settings"))
        row = UtskickSettings.objects.get(account=self.account)
        self.assertEqual(
            row.consent_text_email, "Ja, jag vill få nyheter från Exempelrör via e-post."
        )
        self.assertEqual(row.pref_email_note, "Ungefär en gång i månaden.")
        self.assertEqual(
            (row.display_name, row.public_slug, row.is_enabled), ("Exempelrör", "exempelror", True)
        )

    def test_the_company_name_and_https_are_required(self):
        response = self.client.post(
            self.url("app_contacts_settings"), self.data(consent_text_sms="Ja tack till sms.")
        )
        self.assertContains(response, "Skriv Exempelrör i texten")
        response = self.client.post(
            self.url("app_contacts_settings"), self.data(privacy_url="http://exempelror.example/p")
        )
        self.assertContains(response, "Adressen ska börja med https://")

    def test_the_landing_page_boxes_say_why_they_are_off(self):
        response = self.client.get(self.url("app_contacts_settings"))
        self.assertContains(response, "Ingen integritetstext finns än.")
        self.assertContains(response, "Fyll i organisationsnummer och kontaktuppgifter")
        self.client.post(
            self.url("app_contacts_settings"),
            self.data(privacy_url="https://exempelror.example/integritet"),
        )
        response = self.client.get(self.url("app_contacts_settings"))
        self.assertContains(response, "Visas i formulären för sms.")
        # Varför e-post saknas, och vad kunden kan göra.
        self.assertContains(response, "bekräftelsemejlen är inte påslagna. Be ADX slå på dem.")
        self.client.post(
            self.url("app_contacts_settings"),
            self.data(privacy_url="https://exempelror.example/integritet", lp_consent=""),
        )
        response = self.client.get(self.url("app_contacts_settings"))
        self.assertContains(response, "Av. Formulären på landningssidorna har inga kryssrutor.")

    def test_read_only_rows(self):
        response = self.client.get(self.url("app_contacts_settings"))
        self.assertContains(response, "Exempelrör · Be ADX ändra det.")
        self.assertContains(response, "/utskick/exempelror/")
        self.assertContains(response, "Godkänt av Anna Lindqvist")
        self.assertContains(response, "Ingen export än.")


class ReviewUxTests(ViewsFixture, TestCase):
    """Rättningarna efter UX-granskningen av S1."""

    def test_the_sub_nav_summary_never_says_kontakter_twice(self):
        html = self.client.get(self.url("app_contacts")).content.decode()
        summary = re.search(r'<summary class="fl-subnav__summary">([^<]*)</summary>', html)
        self.assertEqual(summary.group(1), "Kontakter")
        html = self.client.get(self.url("app_lists")).content.decode()
        self.assertIn('<summary class="fl-subnav__summary">Kontakter: Listor</summary>', html)

    def test_new_list_in_the_header_opens_the_form(self):
        html = self.client.get(self.url("app_lists")).content.decode()
        self.assertIn('href="?ny=1#ny-lista"', html)
        self.assertNotRegex(html, r'<details class="fl-kt-more" id="ny-lista" open>')
        html = self.client.get(self.url("app_lists") + "?ny=1").content.decode()
        self.assertRegex(html, r'<details class="fl-kt-more" id="ny-lista" open>')

    def test_import_steps_reach_screen_readers_and_say_done(self):
        from .app_views.imports import _steps

        html = self.client.get(self.url("app_import")).content.decode()
        line = re.search(r'<p class="fl-im-steps__line"[^>]*>', html).group(0)
        self.assertNotIn("aria-hidden", line)
        self.assertEqual(_steps(4)["line"], "Klart: alla fyra steg")
        self.assertEqual(_steps(1)["line"], "Steg 2 av 4: Kolumner")

    def test_earlier_imports_show_a_state_not_a_step(self):
        ImportJob.objects.filter(pk=self.import_job.pk).update(status="review", row_count=3)
        html = self.client.get(self.url("app_import")).content.decode()
        self.assertIn('fl-im-badge--review">Väntar på dig</span>', html)
        self.assertNotIn('fl-im-badge--review">Granska</span>', html)

    def test_bulk_delete_names_what_goes(self):
        response = self.client.post(
            self.url("app_contacts_bulk"), {"action": "delete", "ids": [self.kontakt.pk]}
        )
        self.assertContains(response, ">Ta bort 1 kontakt</button>")

    def test_a_missing_agreement_page_is_said_plainly(self):
        Contact.objects.filter(account=self.account).delete()
        DpaAcceptance.objects.filter(account=self.account).delete()
        html = self.client.get(self.url("app_contacts")).content.decode()
        self.assertIn(self.url("app_dpa") + "?nasta=ny", html)
        html = self.client.get(self.url("app_import")).content.decode()
        self.assertIn(self.url("app_dpa") + "?nasta=import", html)
        DpaVersion.objects.update(is_current=False)
        for name in ("app_contacts", "app_import"):
            with self.subTest(view=name):
                response = self.client.get(self.url(name))
                self.assertContains(response, "Avtalssidan saknas")
                self.assertNotContains(response, "Till biträdesavtalet")
                self.assertNotContains(response, "Godkänn biträdesavtalet för att")

    def test_the_agreement_box_is_a_region(self):
        html = self.client.get(self.url("app_dpa")).content.decode()
        self.assertRegex(html, r'<div class="fl-kt-dpa" role="region" tabindex="0" aria-label=')

    def test_help_and_errors_are_tied_to_the_field(self):
        html = self.client.get(self.url("app_signup")).content.decode()
        self.assertIn('aria-describedby="kt-an-title_helptext"', html)
        self.assertIn('id="kt-an-title_helptext"', html)
        response = self.client.post(self.url("app_signup"), {"title": ""})
        html = response.content.decode()
        tag = re.search(r'<input type="text" name="title"[^>]*>', html).group(0)
        self.assertIn('aria-invalid="true"', tag)
        self.assertIn('aria-describedby="kt-an-title_helptext kt-an-title_error"', tag)
        self.assertIn('id="kt-an-title_error"', html)

    def test_the_login_list_never_says_kontakter(self):
        html = self.client.get(reverse("flamingo:app_settings")).content.decode()
        self.assertNotIn("Inga kontakter är registrerade.", html)

    def test_the_timeline_names_the_file(self):
        contacts.record_event(
            self.kontakt, "imported", {"import": self.import_job.pk, "fil": "kunder.csv"}
        )
        html = self.client.get(self.url("app_contact", self.kontakt.pk)).content.decode()
        self.assertIn("kunder.csv", html)
        self.assertNotRegex(html, r"Import \d")


class PruneTests(ViewsFixture, TestCase):
    def test_only_flagged_contacts_without_basis_are_removed(self):
        from django.utils import timezone

        bo = make_contact(self.account, first_name="Bo", phone=PHONE_BO)
        consent.set_status(bo, "sms", "existing", source="manual", evidence="kassan")
        Contact.objects.filter(pk__in=[self.kontakt.pk, bo.pk]).update(
            inactive_flagged_at=timezone.now()
        )
        html = self.client.get(self.url("app_contacts")).content.decode()
        self.assertIn("1 kontakt saknar samtycke och har inte hörts av på två år.", html)
        response = self.client.get(self.url("app_contacts_prune"))
        self.assertContains(response, "Anna Lind")
        self.assertNotContains(response, "Bo</a>")
        self.client.post(self.url("app_contacts_prune"), {"bekrafta": "1"})
        self.assertFalse(Contact.objects.filter(pk=self.kontakt.pk).exists())
        self.assertTrue(Contact.objects.filter(pk=bo.pk).exists())
        self.assertFalse(Suppression.objects.filter(account=self.account).exists())


# ---------------------------------------------------------------------------
# Mallarna
# ---------------------------------------------------------------------------


class TemplateGuardTests(TestCase):
    def test_no_inline_styles_or_scripts(self):
        for path in OWN_TEMPLATES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("style=", text)
                self.assertNotRegex(text, r"<script(?![^>]*\ssrc=)")

    def test_every_cell_has_a_label(self):
        for path in OWN_TEMPLATES:
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<td\b[^>]*>", text):
                with self.subTest(path=path.name, cell=match.group(0)):
                    self.assertIn("data-label=", match.group(0))

    def test_no_exclamation_marks_or_brackets_in_the_copy(self):
        for path in OWN_TEMPLATES:
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"{% comment %}.*?{% endcomment %}", "", text, flags=re.S)
            text = re.sub(r"{%.*?%}|{{.*?}}|{#.*?#}", "", text, flags=re.S)
            text = re.sub(r"<[^>]+>", "", text)
            with self.subTest(path=path.name):
                self.assertNotIn("!", text)
                self.assertNotRegex(text, r"\[\s*\]")
