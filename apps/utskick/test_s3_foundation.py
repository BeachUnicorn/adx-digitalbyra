"""
Grunden för S3 (README J S3): modellerna och migreringen (B.0 och
raderingsregeln i databasen), token och adresserna för mejlen (E.2),
adresserna till stubbarna (I.1, E.1), inställningarna (C.4), skripten för
AWS (H.8, J S3 steg 1 till 3) och modulernas signaturer (S3-HANDOFF.md).

    ModelTests          e-postens kolumner, domänerna, bilderna, kvittona
    DbDefaultTests      varje ny kolumn på en äldre tabell har ett standardvärde i Postgres
    DbOnDeleteTests     de nya främmande nycklarnas regler, och en äldre version som raderar
    TokenTests          mejlens token: äkta, förfalskade, utgångna, rätt slag
    LinkUrlTests        adresserna på klick.adx.se och List-Unsubscribe
    AppRouteTests       varje S3-adress i verktyget: kontots, 404 annars, POST-regler
    LinkHostRouteTests  /m/, /a/, /v/, /w/, /o/, /c/ bara på klick, utan kakor och CSRF
    ManageRouteTests    byråns S3-adresser bara för byrån
    SettingsTests       standardvärdena, .env.example och testkörningens värden
    QueueHelperTests    köernas inställningar, DLQ:ns namn, kvittots nyckel
    ScriptTests         aws-utskick-role.sh och aws-utskick-s3.sh mot en attrapp av aws
    SignatureTests      modulerna och namnen som byggarna anropar
"""

import importlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models import ForeignKey, RestrictedError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from apps.flamingo.models import MediaAsset

from . import dbfk, links, nav, tokens
from .email import mime
from .inbound import events, queues
from .models import EmailImage, EventReceipt, SenderDomain, Utskick, UtskickSettings
from .testing import UtskickFixture

User = get_user_model()
BASE = Path(settings.BASE_DIR)

LINK_SETTINGS = {
    "UTSKICK_LINK_HOSTS": ["k.adx.se", "klick.adx.se"],
    "UTSKICK_SMS_LINK_BASE": "https://k.adx.se",
    "UTSKICK_EMAIL_LINK_BASE": "https://klick.adx.se",
}
HASH = "ab" * 32
BLOCK_ID = "b_Ab12Cd34Ef56"


def make_utskick(account, **kwargs):
    data = {"name": "Höstservice värmepump", "channel_mode": "email_only"}
    data.update(kwargs)
    return Utskick.objects.create(account=account, **data)


def make_domain(account, domain="exempelror.example", **kwargs):
    data = {"from_name": "Exempelrör"}
    data.update(kwargs)
    return SenderDomain.objects.create(account=account, domain=domain, **data)


# ---------------------------------------------------------------------------
# Modellerna och migreringen
# ---------------------------------------------------------------------------


class ModelTests(UtskickFixture, TestCase):
    def test_an_utskick_gets_the_email_defaults(self):
        utskick = make_utskick(self.account)
        utskick.refresh_from_db()
        self.assertEqual(utskick.subject, "")
        self.assertEqual(utskick.preheader, "")
        self.assertEqual(utskick.email_doc, {})
        self.assertEqual(utskick.email_rev, 0)
        self.assertEqual(utskick.accent, "")
        self.assertEqual(utskick.logo_position, Utskick.LogoPosition.LEFT)
        self.assertIsNone(utskick.sender_domain)
        self.assertEqual(utskick.confirmed_terms, [])
        self.assertFalse(utskick.open_tracking)
        self.assertEqual(utskick.text_override, "")
        self.assertEqual(utskick.email_snapshot, {})
        self.assertTrue(utskick.has_email)
        self.assertFalse(utskick.has_sms)
        both = make_utskick(self.account, channel_mode="both")
        self.assertTrue(both.has_email and both.has_sms)
        sms = make_utskick(self.account, channel_mode="sms_only")
        self.assertFalse(sms.has_email)

    def test_a_domain_and_its_addresses(self):
        domain = make_domain(self.account)
        self.assertEqual(domain.status, SenderDomain.Status.PENDING)
        self.assertEqual(domain.from_address, "hej@exempelror.example")
        self.assertEqual(domain.mail_from_domain, "studs.exempelror.example")
        self.assertFalse(domain.is_verified)
        self.assertNotIn("exempelror", str(domain))

    def test_one_verified_owner_per_domain(self):
        make_domain(self.account, status=SenderDomain.Status.VERIFIED)
        # Väntande anspråk på samma namn får finnas (domains.py avgör dem).
        make_domain(self.other_account)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_domain(self.other_account, status=SenderDomain.Status.VERIFIED)

    def test_a_domain_in_use_cannot_be_deleted_alone(self):
        domain = make_domain(self.account, status=SenderDomain.Status.VERIFIED)
        make_utskick(self.account, sender_domain=domain)
        with self.assertRaises(RestrictedError):
            domain.delete()

    def test_the_account_goes_with_its_domains_and_utskick(self):
        domain = make_domain(self.account, status=SenderDomain.Status.VERIFIED)
        utskick = make_utskick(self.account, sender_domain=domain)
        self.account.delete()
        self.assertFalse(SenderDomain.objects.filter(pk=domain.pk).exists())
        self.assertFalse(Utskick.objects.filter(pk=utskick.pk).exists())

    def test_one_rendition_per_asset_purpose_and_width(self):
        asset = MediaAsset.objects.create(
            account=self.account, file="flamingo/x/a.webp", width=800, height=600
        )
        EmailImage.objects.create(
            account=self.account,
            asset=asset,
            purpose="content",
            file="utskick-img/a/b.jpg",
            format="jpeg",
            width=1120,
        )
        EmailImage.objects.create(
            account=self.account,
            asset=asset,
            purpose="content",
            file="utskick-img/a/c.jpg",
            format="jpeg",
            width=560,
        )
        # Utan asset (bilden togs bort ur arkivet) gäller inget villkor.
        for name in ("d", "e"):
            EmailImage.objects.create(
                account=self.account,
                purpose="content",
                file=f"utskick-img/a/{name}.jpg",
                format="jpeg",
                width=1120,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailImage.objects.create(
                account=self.account,
                asset=asset,
                purpose="content",
                file="utskick-img/a/f.jpg",
                format="jpeg",
                width=1120,
            )

    def test_the_image_path_is_random_and_keeps_no_name(self):
        from .models import email_image_path

        path = email_image_path(None, "Kundens bild Anna.PNG")
        self.assertTrue(path.startswith("utskick-img/"))
        self.assertTrue(path.endswith(".png"))
        self.assertNotIn("Anna", path)
        self.assertTrue(email_image_path(None, "foto.jpeg").endswith(".jpg"))
        self.assertNotEqual(path, email_image_path(None, "Kundens bild Anna.PNG"))

    def test_the_file_goes_with_the_row_after_commit(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            folder = Path(media) / "utskick-img" / "x"
            folder.mkdir(parents=True)
            (folder / "a.jpg").write_bytes(b"jpeg")
            image = EmailImage.objects.create(
                account=self.account, purpose="logo", file="utskick-img/x/a.jpg", format="jpeg"
            )
            with self.captureOnCommitCallbacks(execute=True):
                image.delete()
            self.assertFalse((folder / "a.jpg").exists())

    def test_a_receipt_is_written_once(self):
        EventReceipt.objects.create(key="0100-abc:Delivery")
        with self.assertRaises(IntegrityError), transaction.atomic():
            EventReceipt.objects.create(key="0100-abc:Delivery")
        self.assertEqual(EventReceipt.KEEP_DAYS, 3)

    def test_the_email_health_block_fields(self):
        row = UtskickSettings.objects.get(account=self.account)
        self.assertIsNone(row.email_blocked_at)
        self.assertEqual(row.email_blocked_reason, "")
        self.assertIsNone(row.email_released_at)
        self.assertIsNone(row.email_released_by)

    def test_opened_is_an_s3_event_kind(self):
        from .models import Event

        self.assertEqual(Event.S3_KINDS, ("opened",))


#: Kolumnerna S3 lägger till på tabeller som S2-koden skriver (B.0).
S3_COLUMNS = {
    "utskick_utskick": (
        "subject",
        "preheader",
        "email_doc",
        "email_rev",
        "accent",
        "logo_position",
        "from_name",
        "confirmed_terms",
        "open_tracking",
        "text_override",
        "email_snapshot",
    ),
    "utskick_utskicksettings": ("email_blocked_reason",),
    "utskick_switchboard": ("ses_account",),
}
#: De nullbara (ingen standard behövs).
S3_NULLABLE = {
    "utskick_utskick": ("sender_domain_id", "terms_confirmed_by_id", "terms_confirmed_at"),
    "utskick_utskicksettings": ("email_blocked_at", "email_released_at", "email_released_by_id"),
    "utskick_switchboard": ("ses_checked_at",),
}


class DbDefaultTests(TestCase):
    """B.0 i databasen: S2-koden skriver de här tabellerna utan de nya
    kolumnerna, så varje ny kolumn är nullbar eller har en DEFAULT."""

    def columns(self, table):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = %s",
                [table],
            )
            return {name: (nullable, default) for name, nullable, default in cursor.fetchall()}

    def test_every_new_column_has_a_database_default(self):
        for table, names in S3_COLUMNS.items():
            found = self.columns(table)
            for name in names:
                with self.subTest(column=f"{table}.{name}"):
                    self.assertIn(name, found)
                    self.assertIsNotNone(found[name][1])

    def test_the_rest_are_nullable(self):
        for table, names in S3_NULLABLE.items():
            found = self.columns(table)
            for name in names:
                with self.subTest(column=f"{table}.{name}"):
                    self.assertEqual(found[name][0], "YES")

    def test_an_insert_without_the_new_columns_works(self):
        """Som S2-koden: en rad i utskick_switchboard utan ses_account."""
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO utskick_switchboard (id, sms_enabled, email_enabled, ses_max_rate, "
                "ses_daily_quota, hash_fingerprint, link_fingerprint, last_tick_summary, note) "
                "VALUES (77, false, false, 0, 0, '', '', '{}'::jsonb, '') RETURNING ses_account"
            )
            value = cursor.fetchone()[0]
        self.assertEqual(json.loads(value) if isinstance(value, str) else value, {})


class DbOnDeleteTests(UtskickFixture, TestCase):
    def test_the_migration_lists_every_new_foreign_key(self):
        module = importlib.import_module("apps.utskick.migrations.0003_brev_och_epost")
        listed = set(module.S3_FOREIGN_KEYS)
        found = set()
        for op in module.Migration.operations:
            fields = getattr(op, "fields", None)
            if fields is not None:
                for name, field in fields:
                    if isinstance(field, ForeignKey):
                        found.add(("utskick", op.name.lower(), name))
            field = getattr(op, "field", None)
            if isinstance(field, ForeignKey):
                found.add(("utskick", op.model_name.lower(), op.name))
        self.assertEqual(listed, found)

    def test_each_rule_is_in_the_database(self):
        module = importlib.import_module("apps.utskick.migrations.0003_brev_och_epost")
        from django.apps import apps as django_apps

        for app_label, model_name, field_name in module.S3_FOREIGN_KEYS:
            model = django_apps.get_model(app_label, model_name)
            field = model._meta.get_field(field_name)
            action = dbfk.sql_action(field)
            rules = dbfk.rules(connection, model._meta.db_table)
            with self.subTest(field=f"{model_name}.{field_name}"):
                if action is None:
                    # RESTRICT: ingen regel (NO ACTION, prövas vid commit).
                    self.assertEqual(rules.get(field.column), "a")
                else:
                    self.assertEqual(rules.get(field.column), dbfk.CONFDELTYPE[action])

    def raw_delete(self, table, pk):
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", [pk])
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_an_older_release_can_delete_a_media_asset(self):
        asset = MediaAsset.objects.create(
            account=self.account, file="flamingo/x/a.webp", width=800, height=600
        )
        image = EmailImage.objects.create(
            account=self.account,
            asset=asset,
            purpose="content",
            file="utskick-img/a/b.jpg",
            format="jpeg",
        )
        self.raw_delete("flamingo_mediaasset", asset.pk)
        image.refresh_from_db()
        self.assertIsNone(image.asset_id)

    def test_an_older_release_can_delete_a_user(self):
        user = User.objects.create_user("tillfallig-s3")
        utskick = make_utskick(self.account, terms_confirmed_by=user)
        domain = make_domain(self.account, created_by=user)
        UtskickSettings.objects.filter(account=self.account).update(email_released_by=user)
        self.raw_delete("auth_user", user.pk)
        utskick.refresh_from_db()
        domain.refresh_from_db()
        self.assertIsNone(utskick.terms_confirmed_by_id)
        self.assertIsNone(domain.created_by_id)
        self.assertIsNone(UtskickSettings.objects.get(account=self.account).email_released_by_id)


# ---------------------------------------------------------------------------
# Token och adresser (E.2)
# ---------------------------------------------------------------------------


class TokenTests(SimpleTestCase):
    def test_email_click(self):
        token = tokens.email_click_token(12345, 678)
        self.assertRegex(token, r"^[A-Za-z0-9.]{12,40}$")
        self.assertEqual(tokens.read_email_click(token), tokens.EmailClickRef(12345, 678))
        test = tokens.email_click_token(0, 678)
        self.assertEqual(tokens.read_email_click(test), tokens.EmailClickRef(None, 678))
        self.assertIsNone(tokens.read_email_click(token[:-1] + ("A" if token[-1] != "A" else "B")))
        self.assertIsNone(tokens.read_email_click("1.2"))
        self.assertIsNone(tokens.read_email_click(""))
        # En pixel- eller webbversionstoken duger inte som klick.
        self.assertIsNone(tokens.read_email_click(tokens.pixel_token(12345)))

    def test_unsubscribe_is_its_own_kind(self):
        token = tokens.unsubscribe_token(42, HASH)
        self.assertRegex(token, r"^[A-Za-z0-9._-]{40,120}$")
        ref = tokens.read_unsubscribe(token)
        self.assertEqual((ref.account_id, ref.channel, ref.value_hash), (42, "email", HASH))
        preference = tokens.preference_token(42, "email", HASH)
        self.assertIsNone(tokens.read_unsubscribe(preference))
        self.assertIsNone(tokens.read_preference(token))
        self.assertRegex(preference, r"^[A-Za-z0-9._-]{40,120}$")
        with self.assertRaises(ValueError):
            tokens.unsubscribe_token(42, "")

    def test_web_view_pixel_and_calendar(self):
        view = tokens.web_view_token(7, 99)
        self.assertRegex(view, r"^[A-Za-z0-9.]{16,40}$")
        self.assertEqual(tokens.read_web_view(view), tokens.WebViewRef(7, 99))
        self.assertEqual(tokens.read_web_view(tokens.web_view_token(7)), tokens.WebViewRef(7, None))
        pixel = tokens.pixel_token(99)
        self.assertRegex(pixel, r"^[A-Za-z0-9.]{10,30}$")
        self.assertEqual(tokens.read_pixel(pixel), 99)
        self.assertIsNone(tokens.read_pixel(view))
        calendar = tokens.calendar_token(7, BLOCK_ID)
        self.assertRegex(calendar, r"^[A-Za-z0-9._]{20,60}$")
        self.assertEqual(tokens.read_calendar(calendar), tokens.CalendarRef(7, BLOCK_ID))
        with self.assertRaises(ValueError):
            tokens.calendar_token(7, "b_kort")
        forged = calendar.replace(BLOCK_ID, "b_Zz12Cd34Ef56")
        self.assertIsNone(tokens.read_calendar(forged))

    def test_reply_tokens_fit_and_survive_lower_case(self):
        for kind in tokens.REPLY_KINDS:
            token = tokens.reply_token(kind, 123456, 9876543210)
            with self.subTest(kind=kind):
                self.assertRegex(token, r"^[a-z0-9.]+$")
                self.assertLessEqual(len("s+" + token), 64)
                self.assertEqual(
                    tokens.read_reply_token(token), tokens.ReplyRef(kind, 123456, 9876543210)
                )
                self.assertEqual(tokens.read_reply_token(token.upper()).kind, kind)
        reply = tokens.reply_token(tokens.REPLY, 5, 33)
        # Ett annat slag med samma siffror har en annan signatur.
        self.assertIsNone(tokens.read_reply_token("u" + reply[1:]))
        self.assertIsNone(tokens.read_reply_token(reply[:-1] + ("0" if reply[-1] != "0" else "1")))
        self.assertIsNone(tokens.read_reply_token("r1.2x"))
        with self.assertRaises(ValueError):
            tokens.reply_token("z", 1, 2)

    def test_reply_addresses_on_the_reply_domain_only(self):
        address = tokens.reply_address(tokens.THREAD, 5, 77)
        self.assertTrue(address.startswith("s+t"))
        self.assertTrue(address.endswith("@svar.utskick.adx.se"))
        self.assertEqual(tokens.read_reply_address(address), tokens.ReplyRef("t", 5, 77))
        self.assertEqual(tokens.read_reply_address(address.upper()).object_id, 77)
        other = address.replace("@svar.utskick.adx.se", "@exempelror.example")
        self.assertIsNone(tokens.read_reply_address(other))
        self.assertIsNone(tokens.read_reply_address(address.replace("s+", "x+")))
        self.assertIsNone(tokens.read_reply_address("inte en adress"))

    @override_settings(UTSKICK_REPLY_DOMAIN="svar.example.test")
    def test_the_reply_domain_is_a_setting(self):
        address = tokens.reply_address(tokens.REPLY, 1, 2)
        self.assertTrue(address.endswith("@svar.example.test"))
        self.assertIsNotNone(tokens.read_reply_address(address))

    def test_reply_confirm_is_bound_to_the_address_and_expires(self):
        now = timezone.now()
        token = tokens.reply_confirm_token(9, "Anna@Exempelror.example", now)
        self.assertEqual(tokens.read_reply_confirm(token, "anna@exempelror.example", now), 9)
        self.assertIsNone(tokens.read_reply_confirm(token, "bo@exempelror.example", now))
        later = now + timedelta(days=tokens.REPLY_CONFIRM_DAYS + 1)
        self.assertIsNone(tokens.read_reply_confirm(token, "anna@exempelror.example", later))
        with self.assertRaises(ValueError):
            tokens.reply_confirm_token(9, "", now)


@override_settings(**LINK_SETTINGS)
class LinkUrlTests(SimpleTestCase):
    def path_of(self, url):
        self.assertTrue(url.startswith("https://klick.adx.se/"), url)
        return url[len("https://klick.adx.se") :]

    def view_name(self, url):
        return resolve(self.path_of(url), urlconf="config.urls_links").view_name

    def test_every_email_address_resolves_to_its_view(self):
        class Row:
            def __init__(self, pk):
                self.pk = pk

        cases = {
            links.email_url(None, Row(5), Row(6)): "links:email_click",
            links.email_url(None, None, Row(6)): "links:email_click",
            links.unsubscribe_url(3, HASH): "links:email_unsubscribe",
            links.email_preferences_url(3, HASH): "links:email_preferences",
            links.web_view_url(7, 5): "links:web_view",
            links.web_view_url(7): "links:web_view",
            links.pixel_url(5): "links:open_pixel",
            links.calendar_url(7, BLOCK_ID): "links:calendar",
        }
        for url, name in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.view_name(url), name)

    def test_the_preference_link_is_the_same_token_as_on_adx_se(self):
        url = links.email_preferences_url(3, HASH)
        token = url.rsplit("/", 1)[1]
        ref = tokens.read_preference(token)
        self.assertEqual((ref.account_id, ref.channel), (3, "email"))

    def test_mailto_and_the_list_unsubscribe_headers(self):
        mailto = links.mailto_unsubscribe(3, 5)
        self.assertTrue(mailto.startswith("mailto:s+u"))
        self.assertTrue(mailto.endswith("@svar.utskick.adx.se?subject=avregistrera"))
        address = mailto[len("mailto:") : mailto.index("?")]
        self.assertEqual(tokens.read_reply_address(address), tokens.ReplyRef("u", 3, 5))
        headers = mime.unsubscribe_headers(links.unsubscribe_url(3, HASH), mailto)
        self.assertEqual(headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertRegex(
            headers["List-Unsubscribe"], r"^<https://klick\.adx\.se/a/[^>]+>, <mailto:"
        )
        with self.assertRaises(ValueError):
            mime.unsubscribe_headers("/a/x", mailto)
        with self.assertRaises(ValueError):
            mime.unsubscribe_headers(links.unsubscribe_url(3, HASH), "s+u@x")

    def test_the_headers_survive_the_mime_builder(self):
        from .email.transport import OutgoingMail

        headers = mime.unsubscribe_headers(
            links.unsubscribe_url(3, HASH), links.mailto_unsubscribe(3, 5)
        )
        headers["Reply-To"] = tokens.reply_address(tokens.REPLY, 3, 5)
        raw = mime.build(
            OutgoingMail(
                to="anna@exempelror.example",
                from_name="Exempelrör",
                from_addr="exempelror@utskick.adx.se",
                subject="Höstservice",
                text="Hej",
                html="<p>Hej</p>",
                headers=headers,
            )
        )
        message = mime.parse(raw)
        self.assertEqual(message["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertIn("klick.adx.se/a/", message["List-Unsubscribe"])
        self.assertIn("@svar.utskick.adx.se", message["Reply-To"])


# ---------------------------------------------------------------------------
# Adresserna (I.1, E.1)
# ---------------------------------------------------------------------------

#: (namn, med utskickets pk, metoden stubben svarar på)
APP_ROUTES = [
    ("flamingo:app_brev", True, "get"),
    ("flamingo:app_brev_save", True, "post"),
    ("flamingo:app_brev_render_block", True, "post"),
    ("flamingo:app_brev_image", True, "post"),
    ("flamingo:app_brev_checks", True, "get"),
    ("flamingo:app_brev_ai", True, "post"),
    ("flamingo:app_brev_preview", True, "get"),
    ("flamingo:app_utskick_health", False, "get"),
    ("flamingo:app_utskick_domain", False, "get"),
]
APP_PATHS = {
    "flamingo:app_brev": "/flamingo/app/utskick/{pk}/brev/",
    "flamingo:app_brev_save": "/flamingo/app/utskick/{pk}/brev/spara/",
    "flamingo:app_brev_render_block": "/flamingo/app/utskick/{pk}/brev/rita/",
    "flamingo:app_brev_image": "/flamingo/app/utskick/{pk}/brev/bild/",
    "flamingo:app_brev_checks": "/flamingo/app/utskick/{pk}/brev/kontroller/",
    "flamingo:app_brev_ai": "/flamingo/app/utskick/{pk}/brev/ai/",
    "flamingo:app_brev_preview": "/flamingo/app/utskick/{pk}/brev/forhandsvisning/",
    "flamingo:app_utskick_health": "/flamingo/app/utskick/halsa/",
    "flamingo:app_utskick_domain": "/flamingo/app/utskick/installningar/doman/",
}
#: Svar som en byggd eller obyggd vy får ge för det egna kontot.
OWN_STATUSES = (200, 302, 400, 409, 501)


class AppRouteTests(UtskickFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.utskick = make_utskick(self.account)
        self.foreign = make_utskick(self.other_account)
        self.client = self.client_for(self.anna)

    def url(self, name, with_pk, utskick=None):
        return reverse(name, args=[(utskick or self.utskick).pk] if with_pk else [])

    def test_the_paths_are_the_contracts(self):
        for name, with_pk, _method in APP_ROUTES:
            with self.subTest(name=name):
                self.assertEqual(
                    self.url(name, with_pk), APP_PATHS[name].format(pk=self.utskick.pk)
                )
        url = reverse("flamingo:app_utskick_reply_confirm", args=["abc.def"])
        self.assertEqual(url, "/flamingo/app/utskick/installningar/svarsadress/abc.def/")

    def test_every_route_answers_for_the_own_account(self):
        for name, with_pk, method in APP_ROUTES:
            with self.subTest(name=name):
                response = getattr(self.client, method)(self.url(name, with_pk))
                self.assertIn(response.status_code, OWN_STATUSES)
        url = reverse("flamingo:app_utskick_reply_confirm", args=["abc.def"])
        self.assertIn(self.client.get(url).status_code, OWN_STATUSES)

    def test_another_accounts_utskick_is_404(self):
        for name, with_pk, method in APP_ROUTES:
            if with_pk:
                with self.subTest(name=name):
                    response = getattr(self.client, method)(self.url(name, True, self.foreign))
                    self.assertEqual(response.status_code, 404)

    def test_everything_is_404_when_utskick_is_off(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        for name, with_pk, method in APP_ROUTES:
            with self.subTest(name=name):
                response = getattr(self.client, method)(self.url(name, with_pk))
                self.assertEqual(response.status_code, 404)

    def test_post_routes_refuse_get(self):
        for name, with_pk, method in APP_ROUTES:
            if method == "post":
                with self.subTest(name=name):
                    self.assertEqual(self.client.get(self.url(name, with_pk)).status_code, 405)

    def test_staff_in_view_as_reaches_the_pages(self):
        staff = self.client_for(self.staff)
        for name in ("flamingo:app_utskick_health", "flamingo:app_utskick_domain"):
            with self.subTest(name=name):
                self.assertEqual(staff.get(reverse(name)).status_code, 200)

    def test_the_health_tab(self):
        keys = [key for key, _name, _label in nav.UTSKICK_TABS]
        # S4 lägger Länkar före Leveranshälsa.
        self.assertEqual(keys, ["utskick", "links", "health", "settings"])
        html = self.client.get(reverse("flamingo:app_utskick_health")).content.decode()
        self.assertIn(reverse("flamingo:app_utskick_health"), html)
        self.assertIn('<summary class="fl-subnav__summary">Utskick: Leveranshälsa</summary>', html)


@override_settings(**LINK_SETTINGS)
class LinkHostRouteTests(TestCase):
    TOKENS = {
        "email_click": lambda: tokens.email_click_token(5, 6),
        "email_unsubscribe": lambda: tokens.unsubscribe_token(3, HASH),
        "email_preferences": lambda: tokens.preference_token(3, "email", HASH),
        "web_view": lambda: tokens.web_view_token(7, 5),
        "open_pixel": lambda: tokens.pixel_token(5),
        "calendar": lambda: tokens.calendar_token(7, BLOCK_ID),
    }

    def path(self, name):
        return reverse(f"links:{name}", urlconf="config.urls_links", args=[self.TOKENS[name]()])

    def test_the_paths(self):
        self.assertTrue(self.path("email_click").startswith("/m/"))
        self.assertTrue(self.path("email_unsubscribe").startswith("/a/"))
        self.assertTrue(self.path("email_preferences").startswith("/v/"))
        self.assertTrue(self.path("web_view").startswith("/w/"))
        self.assertRegex(self.path("open_pixel"), r"^/o/.+\.gif$")
        self.assertRegex(self.path("calendar"), r"^/c/.+\.ics$")

    def test_only_on_the_email_host(self):
        for name in self.TOKENS:
            path = self.path(name)
            with self.subTest(name=name):
                self.assertEqual(Client().get(path, HTTP_HOST="k.adx.se").status_code, 404)
                # adx.se har inga sådana adresser alls.
                self.assertEqual(Client().get(path).status_code, 404)

    def test_one_click_needs_no_csrf_and_sets_no_cookie(self):
        client = Client(enforce_csrf_checks=True)
        for name in ("email_unsubscribe", "email_preferences"):
            with self.subTest(name=name):
                response = client.post(
                    self.path(name),
                    data="List-Unsubscribe=One-Click",
                    content_type="application/x-www-form-urlencoded",
                    HTTP_HOST="klick.adx.se",
                    HTTP_ORIGIN="null",
                )
                self.assertNotIn(response.status_code, (403, 405, 500))
                self.assertEqual(response.cookies, {})

    def test_a_get_answers_without_a_cookie(self):
        for name in self.TOKENS:
            with self.subTest(name=name):
                response = Client().get(self.path(name), HTTP_HOST="klick.adx.se")
                self.assertNotIn(response.status_code, (403, 405, 500))
                self.assertEqual(response.cookies, {})
                self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")


class ManageRouteTests(UtskickFixture, TestCase):
    def test_staff_only(self):
        urls = [
            ("post", reverse("manage:utskick_health_release", args=[self.account.pk])),
            ("get", reverse("manage:utskick_domain_admin", args=[1])),
            ("get", reverse("manage:utskick_dlq")),
        ]
        self.assertEqual(urls[0][1], f"/manage/utskick/konto/{self.account.pk}/halsa/")
        self.assertEqual(urls[1][1], "/manage/utskick/doman/1/")
        self.assertEqual(urls[2][1], "/manage/utskick/koer/")
        customer = self.client_for(self.anna)
        staff = Client()
        staff.force_login(self.staff)
        for method, url in urls:
            with self.subTest(url=url):
                self.assertEqual(getattr(customer, method)(url).status_code, 302)
                self.assertEqual(getattr(Client(), method)(url).status_code, 302)
                self.assertNotIn(getattr(staff, method)(url).status_code, (403, 500))
        self.assertEqual(staff.get(urls[0][1]).status_code, 405)

    def test_the_overview_still_renders(self):
        staff = Client()
        staff.force_login(self.staff)
        self.assertEqual(staff.get(reverse("manage:utskick_overview")).status_code, 200)


# ---------------------------------------------------------------------------
# Inställningarna, köerna och skripten
# ---------------------------------------------------------------------------


class SettingsTests(SimpleTestCase):
    def test_s3_defaults(self):
        self.assertEqual(settings.UTSKICK_SES_CONFIGURATION_SET, "adx-utskick")
        self.assertEqual(settings.UTSKICK_ADX_MONTHLY_MAIL_CAP, 2000)
        self.assertEqual(settings.UTSKICK_EMAIL_PER_SECOND, 10)
        self.assertEqual(settings.UTSKICK_REPLY_DOMAIN, "svar.utskick.adx.se")

    def test_the_test_run_never_sees_real_queues(self):
        self.assertEqual(settings.UTSKICK_SQS_EVENTS_URL, "")
        self.assertEqual(settings.UTSKICK_SQS_INBOUND_URL, "")
        self.assertEqual(settings.UTSKICK_SES_INBOUND_BUCKET, "")
        self.assertEqual(settings.UTSKICK_AWS_ROLE_ARN, "")

    def test_every_s3_key_is_in_env_example(self):
        example = (BASE / ".env.example").read_text(encoding="utf-8")
        for name in (
            "UTSKICK_SES_CONFIGURATION_SET",
            "UTSKICK_ADX_MONTHLY_MAIL_CAP",
            "UTSKICK_EMAIL_PER_SECOND",
            "UTSKICK_REPLY_DOMAIN",
            "UTSKICK_SES_INBOUND_BUCKET",
            "UTSKICK_SQS_EVENTS_URL",
            "UTSKICK_SQS_INBOUND_URL",
        ):
            with self.subTest(name=name):
                self.assertIn(name, example)


class QueueHelperTests(SimpleTestCase):
    def test_off_without_settings(self):
        self.assertFalse(queues.enabled())
        self.assertFalse(queues.events_enabled())
        self.assertFalse(queues.inbound_enabled())

    @override_settings(
        UTSKICK_SQS_EVENTS_URL="https://sqs.eu-west-1.amazonaws.com/1/adx-utskick-events",
        UTSKICK_SQS_INBOUND_URL="https://sqs.eu-west-1.amazonaws.com/1/adx-utskick-inbound",
    )
    def test_inbound_also_needs_the_bucket(self):
        self.assertTrue(queues.events_enabled())
        self.assertFalse(queues.inbound_enabled())
        with override_settings(UTSKICK_SES_INBOUND_BUCKET="adx-utskick-inbound-1"):
            self.assertTrue(queues.inbound_enabled())
        self.assertEqual(
            queues.dlq_url(queues.queue_url("events")),
            "https://sqs.eu-west-1.amazonaws.com/1/adx-utskick-events-dlq",
        )
        self.assertEqual(queues.dlq_url(""), "")

    def test_receipt_key_and_tags(self):
        event = {
            "eventType": "Bounce",
            "mail": {"messageId": "0102abc", "tags": {"a": ["12"], "u": ["7"], "r": ["345"]}},
        }
        self.assertEqual(events.receipt_key(event), "0102abc:Bounce")
        self.assertEqual(events.recipient_tags(event), (12, 7, 345))
        self.assertEqual(events.receipt_key({"eventType": "Bounce"}), "")
        self.assertEqual(
            events.recipient_tags({"mail": {"tags": {"r": ["x"]}}}), (None, None, None)
        )
        self.assertNotIn("OPEN", events.EVENT_TYPES)
        self.assertNotIn("CLICK", events.EVENT_TYPES)


FAKE_AWS = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
files = {}
for a in args:
    if a.startswith("file://"):
        with open(a[7:]) as fh:
            files[a] = fh.read()
with open(os.environ["FAKE_AWS_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "files": files}) + "\n")
cmd = " ".join(args[:2])
def opt(name):
    return args[args.index(name) + 1] if name in args else ""
ACC = "500841883756"
if cmd == "sts get-caller-identity":
    print(ACC)
elif cmd == "sesv2 list-email-identities":
    print("adx.se\tnoreply@adx.example" if opt("--region") == "eu-north-1" else "utskick.adx.se")
elif cmd in ("sesv2 get-configuration-set", "sqs get-queue-url", "s3api head-bucket",
             "ses describe-receipt-rule"):
    sys.exit(254)
elif cmd == "sesv2 get-email-identity":
    if "--query" in args:
        print("tok1\ttok2\ttok3")
    else:
        sys.exit(254)
elif cmd == "sns create-topic":
    print("arn:aws:sns:eu-west-1:%s:%s" % (ACC, opt("--name")))
elif cmd == "sqs create-queue":
    print("https://sqs.eu-west-1.amazonaws.com/%s/%s" % (ACC, opt("--queue-name")))
elif cmd == "sqs get-queue-attributes":
    print("arn:aws:sqs:eu-west-1:%s:%s" % (ACC, opt("--queue-url").rsplit("/", 1)[1]))
elif cmd == "sns subscribe":
    print(opt("--topic-arn") + ":sub-1")
elif cmd == "ses describe-active-receipt-rule-set":
    print("None")
"""


class ScriptTests(SimpleTestCase):
    """Skripten körs mot en attrapp av aws (inget nät, inga riktiga
    resurser): rätt anrop, giltig JSON och samma namn i båda."""

    def setUp(self):
        if not shutil.which("bash"):
            self.skipTest("bash saknas")
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        (self.work / "bin").mkdir()
        fake = self.work / "bin" / "aws"
        fake.write_text(FAKE_AWS.replace("/usr/bin/env python3", sys.executable, 1))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        for name in ("aws-utskick-role.sh", "aws-utskick-s3.sh"):
            shutil.copy(BASE / "server" / name, self.work / name)
        self.log = self.work / "log.jsonl"

    def run_script(self, name, **env):
        environ = dict(os.environ)
        environ.update(
            PATH=f"{self.work / 'bin'}{os.pathsep}{environ.get('PATH', '')}",
            FAKE_AWS_LOG=str(self.log),
            **env,
        )
        return subprocess.run(
            ["bash", str(self.work / name)],
            env=environ,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_the_role_policy(self):
        done = self.run_script("aws-utskick-role.sh", UTSKICK_AWS_EXTERNAL_ID="0" * 32)
        self.assertEqual(done.returncode, 0, done.stderr)
        protected = (self.work / "aws-utskick-identities.txt").read_text().split()
        self.assertEqual(
            protected, ["adx.se", "noreply@adx.example", "svar.utskick.adx.se", "utskick.adx.se"]
        )
        policies = {}
        for call in self.calls():
            args = call["args"]
            if args[:2] == ["iam", "put-role-policy"]:
                name = args[args.index("--policy-name") + 1]
                policies[name] = json.loads(args[args.index("--policy-document") + 1])
        statements = {s["Sid"]: s for s in policies["utskick"]["Statement"]}
        self.assertIn(
            "arn:aws:ses:eu-west-1:500841883756:configuration-set/adx-utskick",
            statements["SendFromIdentities"]["Resource"],
        )
        deny = statements["ProtectAdxIdentities"]
        self.assertEqual(deny["Effect"], "Deny")
        for identity in protected:
            self.assertIn(f"arn:aws:ses:*:500841883756:identity/{identity}", deny["Resource"])
        no_send = statements["NoSendFromProtected"]
        self.assertEqual(no_send["Effect"], "Deny")
        self.assertEqual(no_send["Action"], ["ses:SendEmail", "ses:SendRawEmail"])
        patterns = no_send["Condition"]["StringLike"]["ses:FromAddress"]
        self.assertEqual(
            sorted(patterns), ["*@adx.se", "*@svar.utskick.adx.se", "noreply@adx.example"]
        )
        self.assertNotIn("*@utskick.adx.se", patterns)
        queues_allowed = statements["ReadQueues"]["Resource"]
        self.assertEqual(len(queues_allowed), 4)
        self.assertIn("arn:aws:sqs:eu-west-1:500841883756:adx-utskick-events-dlq", queues_allowed)
        self.assertEqual(
            statements["InboundObjects"]["Resource"],
            "arn:aws:s3:::adx-utskick-inbound-500841883756/in/*",
        )
        self.assertEqual(
            policies["utskick-assume"]["Statement"][0]["Resource"],
            "arn:aws:iam::500841883756:role/adx-utskick",
        )
        # En andra körning läser filen och skriver inte om den.
        (self.work / "aws-utskick-identities.txt").write_text("adx.se\nextra.example\n")
        again = self.run_script("aws-utskick-role.sh", UTSKICK_AWS_EXTERNAL_ID="0" * 32)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("extra.example", again.stdout)

    def test_the_role_needs_a_long_external_id(self):
        done = self.run_script("aws-utskick-role.sh", UTSKICK_AWS_EXTERNAL_ID="kort")
        self.assertNotEqual(done.returncode, 0)

    def test_the_resources(self):
        done = self.run_script("aws-utskick-s3.sh")
        self.assertEqual(done.returncode, 0, done.stderr)
        for line in (
            "UTSKICK_SES_CONFIGURATION_SET=adx-utskick",
            "UTSKICK_SQS_EVENTS_URL=https://sqs.eu-west-1.amazonaws.com/500841883756/"
            "adx-utskick-events",
            "UTSKICK_SQS_INBOUND_URL=https://sqs.eu-west-1.amazonaws.com/500841883756/"
            "adx-utskick-inbound",
            "UTSKICK_SES_INBOUND_BUCKET=adx-utskick-inbound-500841883756",
        ):
            self.assertIn(line, done.stdout)
        documents = {}
        commands = []
        for call in self.calls():
            commands.append(" ".join(call["args"][:2]))
            for text in call["files"].values():
                documents.setdefault(" ".join(call["args"][:2]), []).append(json.loads(text))
        destination = documents["sesv2 create-configuration-set-event-destination"][0]
        self.assertEqual(destination["MatchingEventTypes"], list(events.EVENT_TYPES))
        for queue in documents["sqs set-queue-attributes"]:
            self.assertEqual(queue["MessageRetentionPeriod"], "1209600")
            if "RedrivePolicy" in queue:
                redrive = json.loads(queue["RedrivePolicy"])
                self.assertEqual(redrive["maxReceiveCount"], "5")
                self.assertTrue(redrive["deadLetterTargetArn"].endswith("-dlq"))
                json.loads(queue["Policy"])
        rule = documents["ses create-receipt-rule"][0]
        self.assertTrue(rule["ScanEnabled"])
        self.assertEqual(rule["Recipients"], ["svar.utskick.adx.se"])
        self.assertEqual(rule["Actions"][0]["S3Action"]["ObjectKeyPrefix"], "in/")
        records = documents["route53 change-resource-record-sets"][0]["Changes"]
        mx = [c for c in records if c["ResourceRecordSet"]["Type"] == "MX"]
        self.assertEqual(
            mx[0]["ResourceRecordSet"]["ResourceRecords"][0]["Value"],
            "10 inbound-smtp.eu-west-1.amazonaws.com",
        )
        self.assertIn("sesv2 put-configuration-set-suppression-options", commands)
        self.assertIn("ses set-active-receipt-rule-set", commands)

    def test_the_names_agree(self):
        role = (BASE / "server" / "aws-utskick-role.sh").read_text()
        s3 = (BASE / "server" / "aws-utskick-s3.sh").read_text()
        for line in (
            'CONFIG_SET="adx-utskick"',
            'EVENTS_QUEUE="adx-utskick-events"',
            'INBOUND_QUEUE="adx-utskick-inbound"',
            'INBOUND_BUCKET="adx-utskick-inbound-${ACCOUNT}"',
            'INBOUND_PREFIX="in/"',
            'ACCOUNT="500841883756"',
        ):
            with self.subTest(line=line):
                self.assertIn(line, role)
                self.assertIn(line, s3)
        self.assertNotIn('"OPEN"', s3)
        self.assertNotIn('"CLICK"', s3)


# ---------------------------------------------------------------------------
# Modulerna och signaturerna som byggarna anropar (S3-HANDOFF.md)
# ---------------------------------------------------------------------------

#: modul -> {namn: parametrarna i ordning, eller None för en konstant eller klass}
SIGNATURES = {
    "apps.utskick.email.registry": {
        "EMAIL_TYPES": None,
        "BLOCK_KEYS": None,
        "DOC_ELEMENTS": None,
        "MAX_BLOCKS": None,
        "get_type": ["key"],
        "available": ["account", "utskick"],
        "library": ["account", "utskick"],
    },
    "apps.utskick.email.blocks": {
        "SIGN_SALT": None,
        "StaleRevision": None,
        "BlockError": None,
        "new_block": ["type_key", "account", "utskick", "user", "now"],
        "validate": ["account", "utskick", "blocks"],
        "save": ["utskick", "blocks", "rev", "user", "account", "now"],
        "clean_url": ["account", "value", "allow_pending"],
        "parse_rich": ["text", "bold_only"],
        "media_ids": ["blocks"],
        "urls": ["blocks"],
        "terms_from": ["blocks"],
        "active_blocks": ["utskick", "doc"],
    },
    "apps.utskick.email.style": {
        "ACCENT_RE": None,
        "DEFAULT_ACCENT": None,
        "SWATCHES": None,
        "valid_accent": ["value"],
        "default_accent": ["account"],
        "accent_for": ["utskick"],
        "picker": ["account"],
        "palette_for_accent": ["value"],
        "brev_styles": ["palette"],
    },
    "apps.utskick.email.render": {
        "RenderContext": None,
        "LinkSpot": None,
        "MODES": None,
        "context_for": ["utskick", "mode", "recipient", "contact", "test", "snapshot"],
        "render_html": ["utskick", "ctx", "mode"],
        "render_block": ["utskick", "block", "ctx"],
        "snapshot": ["utskick", "now"],
        "collect_links": ["utskick", "doc"],
        "html_size": ["utskick"],
        "web_view": ["utskick", "recipient"],
        "calendar_ics": ["utskick", "block_id"],
        "subject_for": ["utskick", "ctx"],
        "preheader_for": ["utskick", "ctx"],
    },
    "apps.utskick.email.text": {
        "render_text": ["utskick", "ctx"],
        "default_text": ["utskick", "ctx"],
    },
    "apps.utskick.email.images": {
        "rendition": ["asset", "purpose", "width"],
        "logo_for": ["account"],
        "absolute_url": ["image"],
        "uses": ["account_id"],
        "purge_unused": ["now"],
    },
    "apps.utskick.email.checks": {
        "Item": None,
        "MAX_HTML_BYTES": None,
        "email_checks": ["utskick", "now", "contact"],
        "blocking": ["items"],
        "as_json": ["items"],
    },
    "apps.utskick.email.domains": {
        "IN_USE_TEXT": None,
        "DomainRefused": None,
        "Record": None,
        "normalize": ["raw"],
        "claim_problem": ["account", "domain"],
        "claim": ["account", "domain", "from_local", "from_name", "user", "now"],
        "records": ["row"],
        "check": ["row", "now", "alert"],
        "check_due": ["now", "deadline"],
        "remove": ["row", "user", "now"],
        "sendable": ["account", "domain_id"],
        "summary": ["account"],
    },
    "apps.utskick.email.mime": {
        "build": ["mail"],
        "parse": ["raw"],
        "unsubscribe_headers": ["https_url", "mailto_url"],
    },
    "apps.utskick.email.transport": {
        "send": ["mail", "kind", "account_id"],
        "S3_KINDS": None,
        "FakeSes": None,
    },
    "apps.utskick.inbound.queues": {
        "enabled": [],
        "events_enabled": [],
        "inbound_enabled": [],
        "queue_url": ["which"],
        "dlq_url": ["url"],
        "poll_due": ["now"],
        "poll": ["now", "deadline"],
        "dlq_counts": [],
        "redrive": ["which"],
        "check_dlq": ["now"],
    },
    "apps.utskick.inbound.events": {
        "EVENT_TYPES": None,
        "receipt_key": ["event"],
        "recipient_tags": ["event"],
        "apply": ["event", "now"],
    },
    "apps.utskick.inbound.email": {
        "receive": ["notification", "now"],
        "process_pending": ["now", "deadline"],
        "pending_exists": [],
        "sweep_bucket": ["now"],
        "is_autoreply": ["message"],
        "strip_quotes": ["text"],
    },
    "apps.utskick.sending.email": {
        "work_exists": ["now"],
        "send_due": ["now", "deadline", "only"],
        "claim": ["account", "n", "now", "only"],
        "process": ["recipient", "account", "now", "ctx"],
        "adx_cap_left": ["account", "now"],
        "adx_month_count": ["account", "now"],
        "from_for": ["account", "utskick", "sender_domain"],
        "reply_to_for": ["account", "recipient", "thread"],
        "deliver": ["account", "mail", "kind", "utskick", "recipient", "now"],
        "send_test": ["utskick", "address", "contact", "actor", "now"],
        "simulate": ["account", "now", "only"],
        "stale_unknown": ["now"],
    },
    "apps.utskick.sending.health": {
        "utskick_health": ["utskick"],
        "check_utskick": ["utskick", "now"],
        "account_health": ["account", "now"],
        "check_account": ["account", "now"],
        "release": ["account", "actor", "now"],
        "is_blocked": ["account"],
        "probe_state": ["utskick", "now"],
        "daily_cap_left": ["account", "now"],
        "adx_wide": ["now"],
    },
    "apps.utskick.ai": {
        "WRITE_SMS_TOOL": None,
        "WRITE_BLOCK_TOOL": None,
        "AiResult": None,
        "make_utskick_guard": ["account", "utskick"],
        "write_sms": ["utskick", "user", "brief"],
        "write_block": ["utskick", "block_type", "user", "brief", "block"],
    },
    "apps.utskick.app_views.brev": {
        "brev_editor": None,
        "brev_save": None,
        "brev_render_block": None,
        "brev_image": None,
        "brev_checks": None,
        "brev_ai": None,
        "brev_preview": None,
    },
    "apps.utskick.app_views.health": {"utskick_health": None},
    "apps.utskick.app_views.domain": {"utskick_domain": None, "reply_confirm": None},
    "apps.utskick.manage_email": {
        "health_release": None,
        "domain_admin": None,
        "dlq": None,
        "panel_context": ["now"],
    },
    "apps.utskick.link_views": {
        "email_click": None,
        "email_unsubscribe": None,
        "email_preferences": None,
        "web_view": None,
        "open_pixel": None,
        "calendar": None,
    },
}


class SignatureTests(SimpleTestCase):
    def test_every_module_has_its_names(self):
        for module_name, names in SIGNATURES.items():
            module = importlib.import_module(module_name)
            for name, params in names.items():
                with self.subTest(name=f"{module_name}.{name}"):
                    self.assertTrue(hasattr(module, name))
                    if params is None:
                        continue
                    found = list(inspect.signature(getattr(module, name)).parameters)
                    self.assertEqual(found[: len(params)], params)

    def test_the_brev_has_24_elements(self):
        from .email import registry

        self.assertEqual(len(registry.BLOCK_KEYS), 22)
        self.assertEqual(len(set(registry.BLOCK_KEYS)), 22)
        self.assertEqual(len(registry.BLOCK_KEYS) + len(registry.DOC_ELEMENTS), 24)
        self.assertEqual(registry.MAX_BLOCKS, 30)

    def test_the_accent_rule(self):
        from .email import style

        self.assertEqual(style.valid_accent("#1a57d6"), "#1A57D6")
        self.assertEqual(style.valid_accent("1A57D6"), "")
        self.assertEqual(style.valid_accent("#1A57D"), "")
        self.assertEqual(style.DEFAULT_ACCENT, "#1A57D6")
