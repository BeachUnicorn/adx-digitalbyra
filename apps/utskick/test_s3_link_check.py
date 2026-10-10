"""
Mejlets kontroller (README F.5, I.6, H.5) och länkkontrollen som sms och
e-post delar (links.check_destinations, J S3 test_s3_link_check):

    LinkCheckTests      analyzer.fetch med 16 kB och 5 s, privata adresser nekas av
                        den riktiga hämtningen, 15 sekunder totalt, tio körningar per
                        konto och timme, demot hämtar inget, check_links=False
    ChecksTests         det som blockerar (storlek, adress, ämnesrad, domän, taket,
                        värdar, information, obligatoriska fält, tomt mejl), bockarna
                        och varningarna (alt-text, saknade värden, för få poster,
                        villkoren, omdömen, AI-vakten)
"""

import threading
from unittest import mock

from django.test import TestCase, override_settings

from apps.flamingo.models import FlamingoAccount

from . import links
from .email import blocks, checks
from .models import AllowedHost, ContactList, ListMembership, SenderDomain, Utskick
from .test_s3_render import BrevFixture, blk, make_asset

CHECKED = "apps.tools.analyzer.fetch"


def texts(items, level=None):
    return [i.text for i in items if level is None or i.level == level]


def ok_fetch(url, **kwargs):
    return object()


class LinkCheckTests(BrevFixture, TestCase):
    def test_every_destination_goes_through_the_guarded_fetch(self):
        with mock.patch(CHECKED, side_effect=ok_fetch) as fetched:
            items = checks.email_checks(self.utskick)
        urls = {c.args[0] for c in fetched.call_args_list}
        self.assertIn("https://exempelror.example/boka", urls)
        self.assertIn("https://www.youtube.com/watch?v=abc123", urls)
        self.assertTrue(all(u.startswith("https://") for u in urls))
        for call in fetched.call_args_list:
            self.assertEqual(call.kwargs, {"max_bytes": 16384, "time_limit": 5})
        link = next(i for i in items if i.key == "links")
        self.assertEqual(link.level, checks.TICK)
        self.assertEqual(link.text, f"{len(urls)} av {len(urls)} länkar svarar.")

    def test_a_dead_link_warns_with_its_host_only(self):
        def fetch(url, **kwargs):
            if "youtube" in url:
                raise RuntimeError("Connection refused till 10.0.0.1, kropp: hemligt")
            return object()

        with mock.patch(CHECKED, side_effect=fetch):
            items = checks.email_checks(self.utskick)
        link = next(i for i in items if i.key == "links")
        self.assertEqual(link.level, checks.WARNS)
        self.assertIn("Kontrollera länken till www.youtube.com.", link.text)
        self.assertNotIn("hemligt", link.text)
        self.assertNotIn("refused", link.text)

    def test_private_addresses_are_refused_before_any_connection(self):
        with mock.patch("socket.create_connection", side_effect=AssertionError("ansluten")):
            for url in (
                "http://127.0.0.1/",
                "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.5/",
                "http://[::1]/",
            ):
                with self.subTest(url=url):
                    self.assertFalse(links._check_one(url))

    def test_the_15_second_budget(self):
        self.assertEqual(links.CHECK_BUDGET, 15.0)
        release = threading.Event()

        def slow(url, **kwargs):
            release.wait(5)
            return object()

        try:
            with (
                mock.patch.object(links, "CHECK_BUDGET", 0.2),
                mock.patch(CHECKED, side_effect=slow),
            ):
                items = checks.email_checks(self.utskick)
        finally:
            release.set()
        link = next(i for i in items if i.key == "links")
        self.assertEqual(link.text, "Länkarna kontrollerades inte nu. Försök igen senare.")
        self.assertEqual(link.level, checks.WARNS)

    def test_ten_runs_per_account_and_hour(self):
        with mock.patch(CHECKED, side_effect=ok_fetch) as fetched:
            for n in range(links.CHECK_RUNS_PER_HOUR):
                links.check_destinations(self.account, [f"https://exempelror.example/{n}"])
            self.assertEqual(fetched.call_count, links.CHECK_RUNS_PER_HOUR)
            result = links.check_destinations(self.account, ["https://exempelror.example/ny"])
            self.assertEqual(result, {"https://exempelror.example/ny": None})
            self.assertEqual(fetched.call_count, links.CHECK_RUNS_PER_HOUR)
            # Ett annat konto har sin egen gräns.
            links.check_destinations(self.other_account, ["https://exempelror.example/ny"])
            self.assertEqual(fetched.call_count, links.CHECK_RUNS_PER_HOUR + 1)

    def test_at_most_20_destinations(self):
        doc = [
            blk("button", primary_text=f"L{n}", primary_url=f"https://exempelror.example/{n}")
            for n in range(25)
        ]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        with mock.patch(CHECKED, side_effect=ok_fetch) as fetched:
            checks.email_checks(self.utskick)
        self.assertEqual(fetched.call_count, links.CHECK_MAX_URLS)

    def test_the_demo_and_check_links_off_fetch_nothing(self):
        with mock.patch(CHECKED, side_effect=AssertionError("hämtat")):
            items = checks.email_checks(self.utskick, check_links=False)
            self.assertFalse([i for i in items if i.key == "links"])
            FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
            self.utskick.account.refresh_from_db()
            items = checks.email_checks(self.utskick)
        self.assertEqual(next(i for i in items if i.key == "links").level, checks.TICK)


class ChecksTests(BrevFixture, TestCase):
    def run_checks(self, **kwargs):
        kwargs.setdefault("check_links", False)
        self.utskick.refresh_from_db()
        return checks.email_checks(self.utskick, **kwargs)

    def save(self, doc):
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)

    def test_the_full_brev_passes(self):
        items = self.run_checks()
        self.assertEqual(checks.blocking(items), [])
        ticks = texts(items, checks.TICK)
        self.assertIn("Avregistrering i sidfoten och med ett klick i e-postprogrammet.", ticks)
        self.assertIn("Textversionen skapas ur blocken.", ticks)
        self.assertTrue(any(t.startswith("Storlek ") and "av 102 kB" in t for t in ticks))
        self.assertIn(checks.TERMS_TEXT, texts(items, checks.WARNS))
        data = checks.as_json(items)
        self.assertEqual(set(data[0]), {"level", "text", "key"})
        # Det som blockerar står först.
        levels = [i.level for i in items]
        self.assertEqual(levels, sorted(levels, key={"blocks": 0, "tick": 1, "warns": 2}.get))

    def test_subject_tags_address_and_empty(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(subject="", preheader="Hej {fornamn}")
        from apps.flamingo.models import Fact

        Fact.objects.filter(account=self.account, key="adress").delete()
        self.save([])
        blocking = texts(checks.blocking(self.run_checks()))
        self.assertIn("Skriv en ämnesrad.", blocking)
        self.assertIn(checks.ADDRESS_TEXT, blocking)
        self.assertIn(checks.EMPTY_TEXT, blocking)
        self.assertTrue(any(t.startswith("Förhandstexten: {fornamn}") for t in blocking))
        # Information behöver ingen adress i sidfoten.
        Utskick.objects.filter(pk=self.utskick.pk).update(purpose="information")
        self.assertNotIn(checks.ADDRESS_TEXT, texts(self.run_checks()))

    def test_required_fields_and_too_few_items(self):
        self.save([blk("hero", kicker="Bara överrubrik"), blk("columns", items=[{"title": "En"}])])
        items = self.run_checks()
        self.assertIn("Rubrik och bild: fyll i rubrik.", texts(items, checks.BLOCKS))
        self.assertIn("Kolumner: lägg till minst 2 av kolumner.", texts(items, checks.WARNS))

    def test_the_sender_domain(self):
        domain = SenderDomain.objects.create(
            account=self.account, domain="exempelror.example", from_name="Exempelrör"
        )
        Utskick.objects.filter(pk=self.utskick.pk).update(sender_domain=domain)
        self.assertIn(checks.DOMAIN_TEXT, texts(self.run_checks(), checks.BLOCKS))
        SenderDomain.objects.filter(pk=domain.pk).update(status="verified")
        self.assertNotIn(checks.DOMAIN_TEXT, texts(self.run_checks()))

    def test_the_adx_month_cap(self):
        target = "apps.utskick.sending.email.adx_cap_left"
        with mock.patch(target, return_value=0):
            blocking = texts(self.run_checks(), checks.BLOCKS)
        self.assertTrue(any(t.startswith("Taket för ADX-domänen är nått") for t in blocking))
        with mock.patch(target, return_value=760):
            blocking = texts(self.run_checks(email_count=820), checks.BLOCKS)
        self.assertTrue(
            any(t.startswith("Ryms inte i taket: 820 mejl, 760 kvar av 2") for t in blocking)
        )
        with mock.patch(target, return_value=760):
            self.assertFalse([i for i in self.run_checks(email_count=700) if i.key == "sender"])

    def test_link_hosts(self):
        self.save([blk("button", primary_text="Boka", primary_url="https://ny-sajt.example/")])
        self.assertIn(links.PENDING_TEXT, texts(self.run_checks(), checks.BLOCKS))
        AllowedHost.objects.filter(host="ny-sajt.example").update(status="refused")
        self.assertIn(
            "ADX har inte godkänt länkar till ny-sajt.example.",
            texts(self.run_checks(), checks.BLOCKS),
        )
        AllowedHost.objects.filter(host="ny-sajt.example").update(status="approved")
        self.assertFalse([i for i in self.run_checks() if i.key == "hosts"])

    def test_information_rules(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(
            purpose="information", info_reason="arende"
        )
        blocking = texts(self.run_checks(), checks.BLOCKS)
        self.assertIn("Erbjudanden hör inte hemma i information.", blocking)
        self.assertIn(
            "Information kan bara länka till din egen webbplats. Ta bort länken till "
            "www.youtube.com.",
            blocking,
        )
        self.assertIn("Det här ser ut som reklam. Välj Reklam eller ta bort erbjudandet.", blocking)
        # Ett lugnt informationsmejl: bara den egna webbplatsen och kartan.
        self.save(
            [
                blk("heading", text="Ändrade öppettider i november"),
                blk("button", primary_text="Läs mer", primary_url="https://exempelror.example/"),
                blk("hours", show_hours="yes", show_address="yes"),
            ]
        )
        Utskick.objects.filter(pk=self.utskick.pk).update(subject="Nya öppettider", preheader="")
        self.assertEqual(checks.blocking(self.run_checks()), [])
        # En Flamingo-sida är reklam.
        self.save([blk("button", primary_text="Boka", primary_url="https://adx.se/lp/boka/")])
        self.assertIn(
            "Information kan inte länka till en Flamingo-sida. Välj Reklam eller ta bort länken.",
            texts(self.run_checks(), checks.BLOCKS),
        )

    def test_the_agency_override_needs_the_email_fingerprint(self):
        from .sending import checks as sending_checks

        self.save([blk("heading", text="Rabatt för dig som är kund")])
        Utskick.objects.filter(pk=self.utskick.pk).update(
            purpose="information", info_reason="arende", subject="Ditt ärende", preheader=""
        )
        self.utskick.refresh_from_db()
        ad = "Det här ser ut som reklam. Välj Reklam eller ta bort erbjudandet."
        override = {
            "reason": "Avtalat pris i ärendet",
            "fingerprint": sending_checks.content_fingerprint(self.utskick),
        }
        Utskick.objects.filter(pk=self.utskick.pk).update(content_override=override)
        self.assertIn(ad, texts(self.run_checks()))
        override["email_fingerprint"] = checks.email_fingerprint(self.utskick)
        Utskick.objects.filter(pk=self.utskick.pk).update(content_override=override)
        self.assertNotIn(ad, texts(self.run_checks()))
        # Kunden ändrar mejlet: undantaget gäller inte längre.
        self.save([blk("heading", text="Rabatt för alla")])
        self.assertIn(ad, texts(self.run_checks()))

    def test_images_without_alt_text(self):
        bare = make_asset(self.account, alt="")
        self.save([blk("image", image=bare.pk), blk("gallery", items=[{"image": bare.pk}])])
        self.assertIn(
            "2 bilder saknar alt-text. Skriv en beskrivning under Media.",
            texts(self.run_checks(), checks.WARNS),
        )

    def test_missing_merge_values(self):
        listan = ContactList.objects.create(account=self.account, name="Alla")
        for data in (
            {"first_name": "", "email": "a@kund.example"},
            {"first_name": "", "email": "b@kund.example"},
            {"first_name": "Cilla", "email": "c@kund.example"},
            {"first_name": "", "email": "", "phone": "+46701740699"},
        ):
            contact = self.contact(**data)
            ListMembership.objects.create(list=listan, contact=contact)
        Utskick.objects.filter(pk=self.utskick.pk).update(
            audience={"lists": [listan.pk]}, subject="Hej {företag}"
        )
        warns = texts(self.run_checks(), checks.WARNS)
        self.assertIn('2 saknar förnamn, "du" används.', warns)
        self.assertIn(
            "3 saknar företag, där blir det tomt. Skriv en reservtext under Om ett värde saknas "
            "i mejlet.",
            warns,
        )
        anna = self.contact(first_name="", email="d@kund.example")
        warns = texts(self.run_checks(contact=anna), checks.WARNS)
        self.assertIn('Förhandsvisningens kontakt saknar förnamn, "du" används.', warns)

    def test_reviews_that_cannot_be_shown(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(google_place_unverified=True)
        self.utskick.account.refresh_from_db()
        self.assertIn(checks.REVIEWS_TEXT, texts(self.run_checks(), checks.WARNS))

    def test_the_guard_reads_ai_text_only(self):
        block = blk("heading", text="Kundens första rubrik")
        blocks.add_version(
            block,
            {"text": "Vi är bäst i stan", "size": "h2"},
            blocks.SOURCE_AI,
            None,
            account=self.account,
        )
        self.save([block, blk("heading", text="Kundens egen text")])

        class Guard:
            def problems(self, text):
                return ["Lova inga tider."] if "text" not in text else ["Fel"]

        with mock.patch("apps.utskick.ai.make_utskick_guard", return_value=Guard()):
            warns = texts(self.run_checks(), checks.WARNS)
        self.assertIn("Rubrik: Lova inga tider.", warns)
        self.assertNotIn("Rubrik: Fel", warns)

    def test_a_new_text_version_is_a_tick(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(text_override="Egen text")
        self.assertIn("Textversionen är din egen.", texts(self.run_checks(), checks.TICK))


@override_settings(UTSKICK_ADX_MONTHLY_MAIL_CAP=2000)
class FingerprintTests(BrevFixture, TestCase):
    def test_the_fingerprint_follows_the_content(self):
        before = checks.email_fingerprint(self.utskick)
        self.assertEqual(before, checks.email_fingerprint(self.utskick))
        Utskick.objects.filter(pk=self.utskick.pk).update(subject="Ny ämnesrad")
        self.utskick.refresh_from_db()
        self.assertNotEqual(before, checks.email_fingerprint(self.utskick))
