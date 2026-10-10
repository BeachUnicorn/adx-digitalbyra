"""
Brevs register och block (README F.1, F.2, F.3, H.1, J S3 test_s3_registry):

    RegistryTests       de 24 elementen, fältsorterna, schemat som JSON
    AvailabilityTests   låsningarna: information, omdömen, öppettider, loggan
    KindTests           url, date, time, email, phone, code, rich_basic
    RichTests           rich_basic som AST: stycken, fet, kursiv, länkar, listor
    MergeTagTests       okända platshållare, sms-platshållare, fält som saknas
    ValidateTests       blockens form, främmande bilder, låsta nya block, 30 block
    SaveTests           rev, 409, låst läge, signaturen med eget salt, vem som
                        skrev, villkoren, väntande värdar
    NewBlockTests       mallens innehåll ur Företaget och inloggningen
"""

import json
from unittest import mock

from django.core import mail
from django.test import TestCase

from apps.flamingo.models import Fact, FlamingoAccount
from apps.flamingo.pagebuilder import blocks as pb

from . import access, links
from .email import blocks, registry
from .models import AllowedHost, Utskick
from .test_s3_render import BrevFixture, blk, make_asset
from .testing import UtskickFixture


def doc_of(utskick):
    return utskick.email_doc["blocks"]


class RegistryTests(TestCase):
    def test_the_24_elements(self):
        self.assertEqual(tuple(registry.EMAIL_TYPES), registry.BLOCK_KEYS)
        self.assertEqual(len(registry.BLOCK_KEYS) + len(registry.DOC_ELEMENTS), 24)
        names = [t.name for t in registry.TYPES_LIST]
        self.assertEqual(
            names,
            [
                "Rubrik och bild",
                "Rubrik",
                "Text",
                "Knapp (fylld och kantad)",
                "Bild med bildtext",
                "Bild och text",
                "Kolumner",
                "Avdelare",
                "Erbjudande med kod",
                "Tjänster och priser",
                "Omdömen",
                "Så går det till",
                "Datum eller händelse",
                "Kontaktperson",
                "Video",
                "Bildgalleri",
                "Vanliga frågor",
                "Öppettider, adress och karta",
                "Ruta (framhävd text)",
                "Underskrift",
                "Mellanrum",
                "Sociala medier",
            ],
        )

    def test_the_fields_of_f1(self):
        expected = {
            "hero": {
                "image": "media",
                "kicker": "text",
                "title": "text",
                "lead": "textarea",
                "button_text": "text",
                "button_url": "url",
            },
            "text": {"body": "rich_basic"},
            "offer": {"valid_until": "date", "title": "text", "text": "text", "code": "code"},
            "event": {
                "date": "date",
                "start": "time",
                "end": "time",
                "title": "text",
                "place": "text",
                "calendar": "choice",
            },
            "person": {
                "photo": "media",
                "name": "text",
                "role": "text",
                "phone": "phone",
                "email": "email",
            },
            "callout": {"text": "rich_basic"},
            "divider": {},
        }
        for key, fields in expected.items():
            with self.subTest(block=key):
                block_type = registry.get_type(key)
                self.assertEqual({f.key: f.kind for f in block_type.fields}, fields)
        hero = registry.get_type("hero")
        self.assertEqual(hero.field("title").max_length, 90)
        self.assertTrue(hero.field("title").required)
        self.assertEqual(registry.get_type("text").field("body").max_length, 3000)
        steps = registry.get_type("steps").field("items")
        self.assertEqual((steps.min_items, steps.max_items), (2, 5))
        self.assertTrue(steps.sub("text").bold_only)
        self.assertTrue(registry.get_type("callout").field("text").bold_only)
        self.assertEqual(registry.get_type("gallery").field("items").max_items, 4)

    def test_every_kind_is_known(self):
        for block_type in registry.TYPES_LIST:
            for spec in block_type.fields:
                with self.subTest(block=block_type.key, field=spec.key):
                    self.assertIn(spec.kind, registry.FIELD_KINDS)
                    for sub in spec.items:
                        self.assertIn(sub.kind, registry.FIELD_KINDS)

    def test_the_schema_is_json(self):
        schema = json.loads(json.dumps(registry.schema()))
        self.assertEqual(len(schema), 22)
        text = next(t for t in schema if t["key"] == "steps")
        self.assertEqual(text["fields"][1]["min_items"], 2)
        self.assertTrue(text["fields"][1]["items"][0]["bold_only"])
        self.assertEqual(text["variants"], [{"key": "brev", "name": "Brev", "limits": {}}])


class AvailabilityTests(BrevFixture, TestCase):
    def test_everything_for_reklam(self):
        state = registry.available(self.account, self.utskick)
        self.assertTrue(all(ok for ok, _why in state.values()), state)

    def test_offers_are_locked_for_information(self):
        info = Utskick(account=self.account, purpose="information")
        state = registry.available(self.account, info)
        for key in registry.OFFER_KEYS:
            self.assertEqual(state[key], (False, "Erbjudanden hör inte hemma i information."))
        with self.assertRaises(blocks.BlockUnavailable):
            blocks.new_block("offer", self.account, info)

    def test_reviews_need_a_profile(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(google_place_unverified=True)
        self.account.refresh_from_db()
        ok, why = registry.available(self.account, self.utskick)["reviews"]
        self.assertFalse(ok)
        self.assertEqual(why, "Koppla Google-profilen under Företaget.")
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            reco_venue_id="12345", reco_unverified=False
        )
        self.account.refresh_from_db()
        self.assertTrue(registry.available(self.account, self.utskick)["reviews"][0])
        self.assertEqual(registry.review_sources(self.account), {"google": False, "reco": True})

    def test_hours_need_facts(self):
        Fact.objects.filter(account=self.account, key__in=["adress", "oppettider"]).delete()
        ok, why = registry.available(self.account, self.utskick)["hours"]
        self.assertFalse(ok)
        self.assertEqual(why, "Lägg till öppettider eller adress under Företaget.")

    def test_the_library(self):
        info = Utskick(account=self.account, purpose="information")
        library = registry.library(self.account, info)
        self.assertEqual([row["key"] for row in library], list(registry.BLOCK_KEYS))
        offer = next(row for row in library if row["key"] == "offer")
        self.assertFalse(offer["ok"])
        self.assertEqual(offer["why_not"], "Erbjudanden hör inte hemma i information.")
        self.assertEqual(offer["group_name"], "Knappar och erbjudanden")
        json.dumps(library)

    def test_the_logo_choice(self):
        self.assertEqual(registry.logo_state(self.account), (True, ""))
        self.assertEqual(registry.logo_state(self.other_account)[0], False)


class KindTests(BrevFixture, TestCase):
    def clean(self, type_key, **fields):
        block_type = registry.get_type(type_key)
        return blocks.clean_fields(self.account, block_type, fields)

    def error(self, type_key, **fields):
        with self.assertRaises(blocks.BlockError) as caught:
            self.clean(type_key, **fields)
        return [e["text"] for e in caught.exception.errors]

    def test_url_absolute_only(self):
        for value in ("/boka", "#boka", "boka.html", "javascript:alert(1)", "ftp://x.example/a"):
            with self.subTest(value=value):
                self.assertTrue(self.error("hero", button_url=value))
        self.assertEqual(
            self.clean("hero", button_url="mailto:Johan@Exempelror.example")["button_url"],
            "mailto:johan@exempelror.example",
        )
        self.assertEqual(
            self.clean("hero", button_url="tel:08-123 456 78")["button_url"], "tel:+46812345678"
        )

    def test_url_strips_click_ids_and_never_takes_merge_tags(self):
        cleaned = self.clean(
            "hero", button_url="https://exempelror.example/boka?gclid=1&fbclid=2&x=3"
        )["button_url"]
        self.assertEqual(cleaned, "https://exempelror.example/boka?x=3")
        self.assertEqual(
            self.error("hero", button_url="https://exempelror.example/{förnamn}"),
            ["Platshållare går inte i en adress."],
        )
        self.assertTrue(self.error("hero", button_url="https://" + "a" * 500 + ".example/"))

    def test_url_follows_e8(self):
        self.assertTrue(self.error("hero", button_url="https://bit.ly/abc"))
        self.assertTrue(self.error("hero", button_url="https://127.0.0.1/x"))
        self.assertTrue(self.error("hero", button_url="https://klick.adx.se/m/x"))
        # En ny värd sparas (Granska blockerar), en nekad gör det inte.
        self.assertEqual(
            self.clean("hero", button_url="https://ny-sajt.example/")["button_url"],
            "https://ny-sajt.example/",
        )
        AllowedHost.objects.create(
            account=self.account, host="nej.example", status=AllowedHost.Status.REFUSED
        )
        self.assertEqual(
            self.error("hero", button_url="https://nej.example/"),
            ["ADX har inte godkänt länkar till nej.example."],
        )
        with self.assertRaises(links.HostPending):
            blocks.clean_url(self.account, "https://ny-sajt.example/", allow_pending=False)

    def test_date_time_email_phone_code(self):
        fields = self.clean("event", date="2026-10-23", start="9.30", end="18:00", calendar="yes")
        self.assertEqual(
            (fields["date"], fields["start"], fields["end"]), ("2026-10-23", "09:30", "18:00")
        )
        self.assertEqual(self.error("event", date="23 oktober"), ["Skriv datumet som åååå-mm-dd."])
        self.assertEqual(self.error("event", date="2026-02-30"), ["Skriv datumet som åååå-mm-dd."])
        self.assertEqual(self.error("event", start="25:00"), ["Skriv tiden som 15:00."])
        person = self.clean(
            "person", name="Johan", email="Johan@Exempelror.EXAMPLE", phone="070-174 06 05"
        )
        self.assertEqual(person["email"], "johan@exempelror.example")
        self.assertEqual(person["phone"], "+46701740605")
        self.assertEqual(self.clean("person", phone="08-123 456 78")["phone"], "+46812345678")
        self.assertTrue(self.error("person", email="inte en adress"))
        self.assertTrue(self.error("person", phone="123"))
        self.assertEqual(self.clean("offer", code="varme 26")["code"], "VARME26")
        self.assertTrue(self.error("offer", code="ÅÄÖ1"))
        self.assertTrue(self.error("offer", code="AB"))

    def test_text_is_plain_and_has_limits(self):
        cleaned = self.clean("hero", title="<b>Hej</b> " + chr(0x2013) + " du")
        self.assertEqual(cleaned["title"], "Hej - du")
        self.assertEqual(self.error("hero", title="x" * 91), ["Högst 90 tecken (nu 91)."])
        self.assertEqual(self.clean("heading", text="Rubrik", size="")["size"], "h2")
        self.assertTrue(self.error("heading", size="h1"))

    def test_rich_links_are_cleaned_and_checked(self):
        body = self.clean(
            "text", body="Se [sidan](https://exempelror.example/a?gclid=x) och **mer**."
        )["body"]
        self.assertEqual(body, "Se [sidan](https://exempelror.example/a) och **mer**.")
        self.assertTrue(self.error("text", body="Se [här](/relativ)."))
        self.assertEqual(self.error("text", body="x" * 3001), ["Högst 3000 tecken (nu 3001)."])

    def test_items_drop_empty_rows_and_keep_limits(self):
        fields = self.clean(
            "social",
            items=[
                {"network": "facebook", "url": "https://www.facebook.com/x"},
                {"network": "instagram", "url": ""},
            ],
        )
        self.assertEqual(len(fields["items"]), 1)
        self.assertTrue(self.error("columns", items=[{"title": f"K{n}"} for n in range(4)]))
        self.assertTrue(self.error("columns", items=[{"titel": "x"}]))


class RichTests(TestCase):
    def test_paragraphs_bold_italic_links_and_lists(self):
        ast = blocks.parse_rich(
            "Hej **du**, se *här* och [sidan](https://x.example/a).\nNy rad.\n\n- Ett\n- Två"
        )
        self.assertEqual(ast[0]["t"], "p")
        self.assertEqual(
            ast[0]["c"],
            [
                {"t": "text", "v": "Hej "},
                {"t": "b", "c": [{"t": "text", "v": "du"}]},
                {"t": "text", "v": ", se "},
                {"t": "i", "c": [{"t": "text", "v": "här"}]},
                {"t": "text", "v": " och "},
                {"t": "a", "href": "https://x.example/a", "c": [{"t": "text", "v": "sidan"}]},
                {"t": "text", "v": "."},
                {"t": "br"},
                {"t": "text", "v": "Ny rad."},
            ],
        )
        self.assertEqual(
            ast[1], {"t": "ul", "items": [[{"t": "text", "v": "Ett"}], [{"t": "text", "v": "Två"}]]}
        )
        self.assertEqual(blocks.rich_links(ast), ["https://x.example/a"])

    def test_bold_only_keeps_the_rest_as_text(self):
        ast = blocks.parse_rich(
            "**PS.** Se *här* och [sidan](https://x.example).\n- rad", bold_only=True
        )
        self.assertEqual(len(ast), 1)
        nodes = ast[0]["c"]
        self.assertEqual(nodes[0], {"t": "b", "c": [{"t": "text", "v": "PS."}]})
        self.assertNotIn("a", [n["t"] for n in nodes])
        self.assertIn("*här*", blocks.rich_plain(ast))
        self.assertIn("- rad", blocks.rich_plain(ast))

    def test_no_markup_ever(self):
        ast = blocks.parse_rich("<script>x</script> 5 * 3 = 15 och ** ingen fet**")
        self.assertEqual(blocks.rich_plain(ast), "<script>x</script> 5 * 3 = 15 och ** ingen fet**")
        self.assertEqual(blocks.parse_rich(""), [])


class MergeTagTests(BrevFixture, TestCase):
    def test_tags_in_text(self):
        self.assertEqual(blocks.merge_problems(self.account, "Hej {förnamn|du} på {företag}"), [])
        problems = blocks.merge_problems(self.account, "Hej {fornamn} {länk:boka} {avregistrering}")
        self.assertEqual(len(problems), 3)
        self.assertIn("{fornamn} är ingen platshållare", problems[0])
        self.assertIn("går bara i sms", problems[1])
        self.assertIn("Avregistreringen står alltid i sidfoten", problems[2])
        self.assertIn(
            "Fältet {fält:regnr} finns inte", blocks.merge_problems(self.account, "{fält:regnr}")[0]
        )
        from .models import FieldDef

        FieldDef.objects.create(account=self.account, key="regnr", label="Regnummer")
        self.assertEqual(blocks.merge_problems(self.account, "{fält:regnr}"), [])

    def test_an_unknown_tag_stops_the_save(self):
        doc = [blk("heading", text="Hej {fornamn}")]
        with self.assertRaises(blocks.BlockError) as caught:
            blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        error = caught.exception.errors[0]
        self.assertEqual(error["block"], doc[0]["id"])
        self.assertEqual(error["field"], "text")
        self.assertIn("Block 1 (Rubrik), Rubrik", error["where"])


class ValidateTests(BrevFixture, TestCase):
    def test_a_foreign_image_is_a_400(self):
        foreign = make_asset(self.other_account)
        with self.assertRaises(access.ForeignIds):
            blocks.validate(self.account, self.utskick, [blk("image", image=foreign.pk)])
        gallery = blk("gallery", items=[{"image": foreign.pk, "alt": "x"}])
        with self.assertRaises(access.ForeignIds):
            blocks.validate(self.account, self.utskick, [gallery])

    def test_an_old_version_with_a_missing_image_is_dropped(self):
        block = blk("image", image=self.photo.pk)
        old = dict(block["versions"][0], id=pb.new_version_id(), fields={"image": 999999})
        block["versions"].insert(0, old)
        clean = blocks.validate(self.account, self.utskick, [block])
        self.assertEqual(len(clean[0]["versions"]), 1)

    def test_shape_errors(self):
        bad = blk("heading", text="x")
        bad["id"] = "nope"
        bad["extra"] = 1
        with self.assertRaises(blocks.BlockError) as caught:
            blocks.validate(self.account, self.utskick, [bad, {"type": "okänd"}, "x"])
        texts = " ".join(e["text"] for e in caught.exception.errors)
        self.assertIn("Blockets id", texts)
        self.assertIn("Okända nycklar extra", texts)
        self.assertIn("Okänd blocktyp", texts)
        with self.assertRaises(blocks.BlockError):
            blocks.validate(self.account, self.utskick, "inte en lista")

    def test_at_most_30_blocks(self):
        doc = [blk("divider") for _ in range(registry.MAX_BLOCKS + 1)]
        with self.assertRaises(blocks.BlockError) as caught:
            blocks.validate(self.account, self.utskick, doc)
        self.assertIn("Högst 30 block", caught.exception.errors[0]["text"])

    def test_a_new_locked_block_is_refused_and_an_old_one_stays(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(purpose="information")
        self.utskick.refresh_from_db()
        offer = next(b for b in doc_of(self.utskick) if b["type"] == "offer")
        blocks.validate(self.account, self.utskick, [offer])
        with self.assertRaises(blocks.BlockError) as caught:
            blocks.validate(self.account, self.utskick, [blk("prices", title="Priser")])
        self.assertEqual(
            caught.exception.errors[0]["text"], "Erbjudanden hör inte hemma i information."
        )

    def test_required_fields_do_not_stop_a_draft(self):
        clean = blocks.validate(self.account, self.utskick, [blk("hero"), blk("event")])
        self.assertEqual(len(clean), 2)


class SaveTests(BrevFixture, TestCase):
    def test_the_revision(self):
        rev = self.utskick.email_rev
        doc = doc_of(self.utskick)
        new = blocks.save(self.utskick, doc, rev=rev, user=self.anna)
        self.assertEqual(new, rev + 1)
        self.utskick.refresh_from_db()
        self.assertEqual(self.utskick.email_rev, rev + 1)
        with self.assertRaises(blocks.StaleRevision) as caught:
            blocks.save(self.utskick, doc, rev=rev, user=self.anna)
        self.assertEqual(caught.exception.current, rev + 1)

    def test_only_while_editable(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(status="sending")
        with self.assertRaises(blocks.BlockError) as caught:
            blocks.save(self.utskick, doc_of(self.utskick), rev=self.utskick.email_rev)
        self.assertEqual(caught.exception.errors[0]["text"], "Utskicket går inte att ändra nu.")

    def test_versions_are_signed_with_the_email_salt(self):
        for block in doc_of(self.utskick):
            for version in block["versions"]:
                with self.subTest(block=block["type"]):
                    self.assertTrue(blocks.is_signed(version))
                    self.assertFalse(pb.is_signed(version))
        # Sidornas signatur ändras inte av e-postens salt.
        page_version = pb.sign_version(
            {
                "id": "v_abcdefghijkl",
                "fields": {},
                "source": "ai",
                "by": None,
                "at": "2026-10-10T00:00:00+00:00",
            }
        )
        self.assertTrue(pb.is_signed(page_version))
        self.assertFalse(blocks.is_signed(page_version))

    def test_a_copied_ai_version_becomes_the_customers(self):
        version = pb.sign_version(
            {
                "id": pb.new_version_id(),
                "fields": {"text": "Från en sida", "size": "h2"},
                "source": "ai",
                "by": None,
                "at": "2026-10-10T00:00:00+00:00",
            }
        )
        block = {
            "id": pb.new_block_id(),
            "type": "heading",
            "variant": "brev",
            "active": version["id"],
            "versions": [version],
        }
        blocks.save(self.utskick, [block], rev=self.utskick.email_rev, user=self.anna)
        saved = doc_of(self.utskick)[0]["versions"][0]
        self.assertEqual(saved["source"], "customer")
        self.assertEqual(saved["by"], self.anna.pk)
        # Byrån i kundvyn står som ADX; en oförändrad version behåller sin källa.
        edited = json.loads(json.dumps(doc_of(self.utskick)))
        edited[0]["versions"][0]["fields"]["text"] = "Ändrad av byrån"
        blocks.save(self.utskick, edited, rev=self.utskick.email_rev, user=self.staff)
        self.assertEqual(doc_of(self.utskick)[0]["versions"][0]["source"], "adx")
        blocks.save(self.utskick, doc_of(self.utskick), rev=self.utskick.email_rev, user=self.anna)
        self.assertEqual(doc_of(self.utskick)[0]["versions"][0]["source"], "adx")

    def test_a_template_block_keeps_its_source(self):
        block = blocks.new_block("heading", self.account, self.utskick, user=self.anna)
        blocks.save(self.utskick, [block], rev=self.utskick.email_rev, user=self.anna)
        self.assertEqual(doc_of(self.utskick)[0]["versions"][0]["source"], "template")

    def test_the_terms(self):
        terms = self.utskick.confirmed_terms
        self.assertIn({"label": "Kod", "value": "VARME26"}, terms)
        self.assertIn({"label": "Erbjudandet gäller", "value": "till 31 oktober"}, terms)
        self.assertIsNone(self.utskick.terms_confirmed_at)
        blocks.save(
            self.utskick,
            doc_of(self.utskick),
            rev=self.utskick.email_rev,
            user=self.anna,
            terms_ok=True,
        )
        self.utskick.refresh_from_db()
        self.assertIsNotNone(self.utskick.terms_confirmed_at)
        self.assertEqual(self.utskick.terms_confirmed_by, self.anna)
        # Samma villkor: bekräftelsen står kvar. Nya villkor: den nollas.
        blocks.save(self.utskick, doc_of(self.utskick), rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        self.assertIsNotNone(self.utskick.terms_confirmed_at)
        doc = json.loads(json.dumps(doc_of(self.utskick)))
        offer = next(b for b in doc if b["type"] == "offer")
        offer["versions"][0]["fields"]["code"] = "VINTER27"
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        self.assertIsNone(self.utskick.terms_confirmed_at)
        self.assertIn({"label": "Kod", "value": "VINTER27"}, self.utskick.confirmed_terms)

    def test_a_new_host_waits_for_adx_and_alerts_once(self):
        mail.outbox = []
        doc = [blk("button", primary_text="Boka", primary_url="https://ny-sajt.example/boka")]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        row = AllowedHost.objects.get(account=self.account, host="ny-sajt.example")
        self.assertEqual(row.status, AllowedHost.Status.PENDING)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("anna@exempelror.example", mail.outbox[0].to)
        blocks.save(self.utskick, doc_of(self.utskick), rev=self.utskick.email_rev, user=self.anna)
        self.assertEqual(len(mail.outbox), 1)

    def test_media_ids_cover_every_version(self):
        ids = blocks.media_ids(doc_of(self.utskick))
        self.assertEqual(ids, {self.photo.pk, self.other.pk})
        # Det active_blocks ger fungerar lika bra (frysningen anropar så).
        self.assertEqual(blocks.media_ids(blocks.active_blocks(self.utskick)), ids)
        self.assertEqual(
            blocks.urls(blocks.active_blocks(self.utskick)), blocks.urls(doc_of(self.utskick))
        )

    def test_add_version(self):
        block = doc_of(self.utskick)[1]
        version = blocks.add_version(
            block, {"text": "Ny rubrik", "size": "h3"}, blocks.SOURCE_AI, None, account=self.account
        )
        self.assertEqual(block["active"], version["id"])
        self.assertTrue(blocks.is_signed(version))
        with self.assertRaises(blocks.BlockError):
            blocks.add_version(block, {"text": "{x}"}, blocks.SOURCE_AI, None, account=self.account)


class NewBlockTests(BrevFixture, TestCase):
    def test_every_type_can_be_created_and_saved(self):
        doc = [
            blocks.new_block(key, self.account, self.utskick, user=self.anna)
            for key in registry.BLOCK_KEYS
        ]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.assertEqual(len(doc_of(self.utskick)), 22)

    def test_the_templates_use_the_facts(self):
        prices = blocks.active_fields(blocks.new_block("prices", self.account, self.utskick))
        self.assertEqual(prices["items"], [{"name": "Service, luft-luft", "price": "1 495 kr"}])
        signature = blocks.active_fields(
            blocks.new_block("signature", self.account, self.utskick, user=self.anna)
        )
        self.assertEqual(signature["name"], "Anna Lindqvist")
        self.assertEqual(signature["script_name"], "Anna")
        self.assertEqual(signature["phone"], "+46812345678")
        staff = blocks.active_fields(
            blocks.new_block("signature", self.account, self.utskick, user=self.staff)
        )
        self.assertEqual(staff["name"], "")
        reviews = blocks.active_fields(blocks.new_block("reviews", self.account, self.utskick))
        self.assertEqual(reviews["source"], "google")

    def test_unknown_type(self):
        with self.assertRaises(blocks.BlockError):
            blocks.new_block("form", self.account, self.utskick)


class OtherAccountTests(UtskickFixture, TestCase):
    def test_an_account_without_facts_or_profile(self):
        utskick = Utskick.objects.create(account=self.other_account, name="Test")
        state = registry.available(self.other_account, utskick)
        self.assertFalse(state["reviews"][0])
        self.assertFalse(state["hours"][0])
        with mock.patch.object(registry, "review_sources", return_value={"google": True}):
            self.assertTrue(registry.available(self.other_account, utskick)["reviews"][0])
