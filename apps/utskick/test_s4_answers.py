"""
Svar i formulär i utskicken (Giovanni 2026-10-10, "bygg flervalet med 1-7",
punkt 2 och 7): segmentregeln answer:<sida>.<fråga> och Förvälj svar på
länkarna till en Flamingo-sida (apps/flamingo/answers.py).

    CleanTests          prövningen: kanonisk form, främmande eller borttagen sida
                        (ForeignIds), nyckelns form, borttagen fråga, okänt
                        alternativ, saknat värde (clean och clean_partial)
    CompileTests        in och not_in, skräp och svarstrådar, andra konton (samma
                        sida och nycklar, en främmande förfrågan på kontakten),
                        count, matches_q, for_contact och ett riktigt formulär
    DescribeTests       klartexten, borttagna alternativ, frågor och sidor
    BuilderTests        gruppen Svar i formulär, spara, räkningen, borttagen fråga,
                        främmande sida (400)
    NamedLinkTests      Ny länk och en länks sida med Förvälj svar, prövningen mot
                        sidan, klicket och HEAD, sidan som öppnas med svaret ikryssat
    SlotTests           utskickets sms-länk (lank_forval) och mejlets länk
                        (_tracked_link behåller ?val=)
    ExportTests         flerval i GDPR-utdraget
    GuardTests          texterna

Inget når nätet. Länkvärdarna testas med LINK_SETTINGS och
HTTP_HOST="klick.adx.se".
"""

import copy
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo import answers as form_answers
from apps.flamingo import pagebuilder
from apps.flamingo.exports import landing_page_url
from apps.flamingo.models import Campaign, LandingPage, Lead, Service

from . import contacts, links, segments, tokens
from .access import ForeignIds
from .app_views import links as link_views
from .app_views import segments as segment_views
from .models import Click, Contact, Segment, TrackedLink, Utskick
from .sending import email as sending_email
from .test_s2_links import IPHONE, LinkFixture, query
from .test_s4_foundation import LINK_SETTINGS, make_named
from .testing import make_contact

KLICK = {"HTTP_HOST": "klick.adx.se"}
LABEL = "Vilken tjänst önskar du?"
#: Giovannis exempel: flera svar, krävs.
TJANST = {
    "key": "tjanst",
    "label": LABEL,
    "kind": "many",
    "options": "Bilservice\nReparation\nFelsökning",
    "required": "required",
}
#: Ett svar, valfritt.
NAR = {
    "key": "nar",
    "label": "När passar det?",
    "kind": "one",
    "options": "I veckan\nPå helgen",
    "required": "",
}
#: En äldre textfråga: inget flerval.
OLD = {"key": "storlek", "label": "Ungefär hur stort?", "kind": "text"}
NAMES = {"bilservice": "Bilservice", "reparation": "Reparation", "felsokning": "Felsökning"}
NEW_LINK_URL = reverse("flamingo:app_link_new")


class AnswerFixture(LinkFixture):
    """LinkFixture (Exempelrör med Badrum Nacka och Rörjour Nacka) där
    Badrum Nacka visar sidan Bilservice med två flerval och en textfråga,
    och ett annat konto med en sida som har samma frågenycklar."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.page = cls.make_page(cls.account, "Bilservice", [TJANST, NAR, OLD])
        Campaign.objects.filter(pk=cls.quote_page.pk).update(landing_page=cls.page)
        cls.quote_page.refresh_from_db()
        cls.other_page = cls.make_page(cls.other_account, "Hemlig", [TJANST])
        other_service = Service.objects.create(
            account=cls.other_account, name="Bilservice", sales_mode=Service.SALES_QUOTE
        )
        cls.other_campaign = Campaign.objects.create(
            account=cls.other_account,
            service=other_service,
            name="Hemlig bilservice",
            status=Campaign.STATUS_LIVE,
            landing_page=cls.other_page,
        )

    @classmethod
    def make_page(cls, account, name, questions):
        hero = pagebuilder.new_block("hero", "form", account)
        form = pagebuilder.new_block("form", "questions", account)
        fields = pagebuilder.active_fields(form)
        fields["questions"] = copy.deepcopy(questions)
        pagebuilder.add_version(form, fields, pagebuilder.SOURCE_CUSTOMER, cls.anna)
        blocks = [hero, form]
        return LandingPage.objects.create(
            account=account,
            name=name,
            draft={"blocks": blocks},
            published={"blocks": copy.deepcopy(blocks)},
            published_at=timezone.now(),
        )

    def set_questions(self, page, questions):
        """Byt sidans frågor (publicerat och utkast)."""
        blocks = copy.deepcopy(page.published_blocks)
        form = next(b for b in blocks if b["type"] == "form")
        fields = pagebuilder.active_fields(form)
        fields["questions"] = copy.deepcopy(questions)
        pagebuilder.add_version(form, fields, pagebuilder.SOURCE_CUSTOMER, self.anna)
        LandingPage.objects.filter(pk=page.pk).update(
            draft={"blocks": blocks}, published={"blocks": copy.deepcopy(blocks)}
        )
        page.refresh_from_db()

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.anna)
        self.n = 0

    # --- kontakter och förfrågningar -----------------------------------------

    def contact(self, account=None, **data):
        self.n += 1
        account = account or self.account
        data.setdefault("first_name", f"Person{self.n}")
        data.setdefault("phone", f"+4670111{self.n:04d}")
        return make_contact(account, **data)

    def entry(self, *o, q="tjanst", page=None, label=LABEL, multi=True):
        return {
            "page": self.page.pk if page is None else page,
            "q": q,
            "label": label,
            "multi": multi,
            "o": list(o),
            "labels": [NAMES.get(k, k) for k in o],
        }

    def lead(self, contact, *entries, account=None, **kwargs):
        kwargs.setdefault("source", Lead.SOURCE_FORM)
        return Lead.objects.create(
            account=account or self.account,
            contact=contact,
            name="Test",
            choice_answers=list(entries),
            **kwargs,
        )

    def rule(self, op="in", v=("reparation",), q="tjanst", page=None):
        return {"f": f"answer:{page or self.page.pk}.{q}", "op": op, "v": list(v)}

    def match(self, *rules, account=None):
        found = segments.contacts(account or self.account, {"all": list(rules)})
        return set(found.values_list("pk", flat=True))


# ---------------------------------------------------------------------------
# Prövningen
# ---------------------------------------------------------------------------


class CleanTests(AnswerFixture, TestCase):
    def test_the_canonical_form(self):
        cleaned = segments.clean(
            self.account,
            {"all": [self.rule(v=[" reparation", "reparation", "felsokning", ""])]},
        )
        self.assertEqual(
            cleaned,
            {
                "all": [
                    {
                        "f": f"answer:{self.page.pk}.tjanst",
                        "op": "in",
                        "v": ["reparation", "felsokning"],
                    }
                ]
            },
        )
        cleaned = segments.clean(self.account, {"all": [self.rule("not_in", ["pa-helgen"], "nar")]})
        self.assertEqual(cleaned["all"][0]["v"], ["pa-helgen"])

    def test_a_foreign_or_missing_page_is_foreign_ids(self):
        for page in (self.other_page.pk, 999999):
            with self.subTest(page=page), self.assertRaises(ForeignIds):
                segments.clean(self.account, {"all": [self.rule(page=page)]})
        with self.assertRaises(ForeignIds):
            segments.clean_partial(self.account, {"all": [self.rule(page=self.other_page.pk)]})

    def test_only_the_pages_the_rules_point_at_are_loaded(self):
        """Förut hämtades alla kontots sidor, med utkast och publicerad
        version, två gånger per räkning (granskningen 2026-10-10)."""
        for n in range(3):
            self.make_page(self.account, f"Annan {n}", [TJANST])
        rules = {
            "all": [
                self.rule(v=["reparation"]),
                self.rule("not_in", ["pa-helgen"], "nar"),
                {"any": [self.rule(v=["bilservice"]), self.rule(v=["felsokning"])]},
            ]
        }
        everything = mock.patch.object(
            form_answers, "questions_for_account", side_effect=AssertionError("alla sidor")
        )
        loads = []
        real = form_answers.questions_by_page

        def counting(account_id, page_ids):
            loads.append(sorted(page_ids))
            return real(account_id, page_ids)

        with everything, mock.patch.object(form_answers, "questions_by_page", counting):
            segments.clean(self.account, rules)
            self.assertEqual(loads, [[self.page.pk]])
            loads.clear()
            segments.clean_partial(self.account, rules)
            segments.describe(self.account, rules)
            segments.count(self.account, rules)
        self.assertTrue(all(ids == [self.page.pk] for ids in loads), loads)

    def test_the_key_must_have_its_form(self):
        for f in ("answer", "answer:", "answer:abc", "answer:0.tjanst", "answer:12", "answer:1.A"):
            with self.subTest(f=f), self.assertRaises(segments.SegmentError) as caught:
                segments.clean(self.account, {"all": [{"f": f, "op": "in", "v": ["x"]}]})
            self.assertEqual(caught.exception.rows, {1: segments.SHAPE_TEXT})

    def test_a_question_that_is_gone_or_hidden(self):
        for q in ("borta", "storlek"):
            with self.subTest(q=q), self.assertRaises(segments.SegmentError) as caught:
                segments.clean(self.account, {"all": [self.rule(q=q)]})
            self.assertEqual(caught.exception.rows, {1: segments.GONE_QUESTION_TEXT})
        # Ett flerval med ett alternativ syns inte på sidan: inget att välja.
        self.set_questions(self.page, [{**TJANST, "options": "Bilservice"}])
        with self.assertRaises(segments.SegmentError) as caught:
            segments.clean(self.account, {"all": [self.rule(v=["bilservice"])]})
        self.assertEqual(caught.exception.rows, {1: segments.GONE_QUESTION_TEXT})

    def test_an_unknown_option_and_the_operators(self):
        with self.assertRaises(segments.SegmentError) as caught:
            segments.clean(self.account, {"all": [self.rule(v=["reparation", "pa-helgen"])]})
        self.assertEqual(caught.exception.rows, {1: segments.ANSWER_CHOICE_TEXT})
        with self.assertRaises(segments.SegmentError) as caught:
            segments.clean(self.account, {"all": [self.rule(op="eq")]})
        self.assertEqual(caught.exception.rows, {1: segments.MISSING_TEXTS["op"]})

    def test_a_missing_answer(self):
        with self.assertRaises(segments.SegmentError) as caught:
            segments.clean(self.account, {"all": [self.rule(v=[])]})
        self.assertEqual(caught.exception.rows, {1: "Välj ett svar."})
        rules, skipped = segments.clean_partial(
            self.account,
            {"all": [{"f": "kind", "op": "eq", "v": "person"}, self.rule(v=[""])]},
        )
        self.assertEqual(skipped, [2])
        self.assertEqual(len(rules["all"]), 1)


# ---------------------------------------------------------------------------
# Kompilatorn
# ---------------------------------------------------------------------------


class CompileTests(AnswerFixture, TestCase):
    def test_in_and_not_in(self):
        a, b, c, d = (self.contact() for _ in range(4))
        self.lead(a, self.entry("bilservice", "reparation"))
        self.lead(b, self.entry("felsokning"), self.entry("pa-helgen", q="nar", multi=False))
        self.lead(c, self.entry("bilservice"))
        everyone = set(Contact.objects.filter(account=self.account).values_list("pk", flat=True))
        self.assertEqual(self.match(self.rule(v=["reparation"])), {a.pk})
        self.assertEqual(self.match(self.rule(v=["reparation", "felsokning"])), {a.pk, b.pk})
        self.assertEqual(self.match(self.rule(v=["pa-helgen"], q="nar")), {b.pk})
        # Valde inte: också de som aldrig svarat (som "Ingen förfrågan").
        self.assertEqual(self.match(self.rule("not_in", ["reparation"])), everyone - {a.pk})
        self.assertIn(d.pk, self.match(self.rule("not_in", ["reparation"])))

    def test_junk_and_reply_leads_do_not_count(self):
        a, b, c = (self.contact() for _ in range(3))
        self.lead(a, self.entry("reparation"), status=Lead.STATUS_JUNK)
        self.lead(b, self.entry("reparation"), source=Lead.SOURCE_REPLY)
        self.lead(c, self.entry("reparation"))
        self.assertEqual(self.match(self.rule()), {c.pk})

    def test_other_accounts_never_match(self):
        mine = self.contact()
        theirs = self.contact(account=self.other_account)
        # Det andra kontots förfrågan med min sidas id och samma nycklar.
        self.lead(theirs, self.entry("reparation"), account=self.other_account)
        # En förfrågan på det andra kontot som pekar på min kontakt.
        self.lead(mine, self.entry("reparation"), account=self.other_account)
        self.assertEqual(self.match(self.rule()), set())
        # Det andra kontots sida i min regel (manipulerad i databasen): ingen.
        self.lead(
            theirs, self.entry("reparation", page=self.other_page.pk), account=self.other_account
        )
        self.assertEqual(self.match(self.rule(page=self.other_page.pk)), set())
        # Det andra kontot ser bara sina egna kontakter.
        self.assertEqual(
            self.match(self.rule(page=self.other_page.pk), account=self.other_account),
            {theirs.pk},
        )

    def test_a_broken_rule_gives_no_one(self):
        a = self.contact()
        self.lead(a, self.entry("reparation"))
        for rule in (
            {"f": "answer:x", "op": "in", "v": ["reparation"]},
            {"f": f"answer:{self.page.pk}.tjanst", "op": "in", "v": ["<script>"]},
            {"f": f"answer:{self.page.pk}.tjanst", "op": "in", "v": []},
            {"f": f"answer:{self.page.pk}.tjanst", "op": "eq", "v": ["reparation"]},
        ):
            with self.subTest(rule=rule):
                self.assertEqual(self.match(rule), set())

    def test_count_matches_q_and_the_chips(self):
        a, b = self.contact(), self.contact()
        self.lead(a, self.entry("reparation"))
        rules = {"all": [self.rule()]}
        counted = segments.count(self.account, rules)
        self.assertEqual(counted["total"], 1)
        segment = Segment.objects.create(account=self.account, name="Valde reparation", rules=rules)
        inside = segments.matches_q(self.account.pk, [segment.pk])
        everyone = Contact.objects.filter(account=self.account)
        self.assertEqual(set(everyone.filter(inside).values_list("pk", flat=True)), {a.pk})
        # Undantaget tappar aldrig den som saknar förfrågningar.
        self.assertIn(b.pk, set(everyone.exclude(inside).values_list("pk", flat=True)))
        self.assertEqual(segments.for_contact(a), [segment])
        self.assertEqual(segments.for_contact(b), [])

    def test_a_saved_rule_keeps_matching_after_the_question_is_gone(self):
        a = self.contact()
        self.lead(a, self.entry("reparation"))
        self.set_questions(self.page, [OLD])
        self.assertEqual(self.match(self.rule()), {a.pk})

    @override_settings(INQUIRY_NOTIFICATION_EMAIL="byran@adx.example")
    def test_a_real_form_post_matches(self):
        response = Client().post(
            self.quote_page.landing_url,
            {
                "name": "Maria Nilsson",
                "phone": "070-111 22 33",
                "email": "maria@hemma.example",
                "q_tjanst": ["reparation", "bilservice"],
                "q_nar": "i-veckan",
            },
            REMOTE_ADDR="10.9.8.7",
        )
        self.assertEqual(response.status_code, 302, response.content.decode()[:2000])
        lead = Lead.objects.get(account=self.account, name="Maria Nilsson")
        kontakt = self.contact()
        Lead.objects.filter(pk=lead.pk).update(contact=kontakt)
        self.assertEqual(self.match(self.rule(v=["reparation"])), {kontakt.pk})
        self.assertEqual(self.match(self.rule(v=["i-veckan"], q="nar")), {kontakt.pk})
        self.assertEqual(self.match(self.rule(v=["felsokning"])), set())


# ---------------------------------------------------------------------------
# Klartexten
# ---------------------------------------------------------------------------


class DescribeTests(AnswerFixture, TestCase):
    def describe(self, *rules):
        return segments.describe(self.account, {"all": list(rules)})

    def test_the_lines(self):
        self.assertEqual(
            self.describe(
                self.rule(v=["reparation", "felsokning"]),
                self.rule("not_in", ["pa-helgen"], "nar"),
            ),
            [
                f'Valde Reparation eller Felsökning i "{LABEL}" (Bilservice)',
                'Valde inte På helgen i "När passar det?" (Bilservice), eller svarade inte',
            ],
        )
        # Flera alternativ i "valde inte": inget av dem.
        self.assertEqual(
            self.describe(self.rule("not_in", ["bilservice", "reparation", "felsokning"])),
            [
                "Valde inget av Bilservice, Reparation och Felsökning "
                f'i "{LABEL}" (Bilservice), eller svarade inte'
            ],
        )

    def test_what_is_gone(self):
        self.assertEqual(
            self.describe(self.rule(v=["reparation", "borta"])),
            [f'Valde Reparation eller ett borttaget svar i "{LABEL}" (Bilservice)'],
        )
        self.assertEqual(
            self.describe(self.rule(q="borta")),
            ["Valde ett borttaget svar i en fråga som inte finns längre (Bilservice)"],
        )
        # Ett annat kontos sida: inget namn hämtas.
        self.assertEqual(
            self.describe(self.rule(page=self.other_page.pk)),
            ["Valde ett borttaget svar i en fråga som inte finns längre"],
        )


# ---------------------------------------------------------------------------
# Byggaren
# ---------------------------------------------------------------------------


class BuilderTests(AnswerFixture, TestCase):
    def test_the_group_and_the_choices(self):
        response = self.client.get(reverse("flamingo:app_segment_new"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<optgroup label="Svar i formulär">', html)
        value = f"answer:{self.page.pk}.tjanst"
        self.assertIn(f'<option value="{value}">Bilservice: {LABEL}</option>', html)
        self.assertIn(f'<option value="answer:{self.page.pk}.nar">', html)
        self.assertNotIn(f"answer:{self.other_page.pk}.", html)
        self.assertNotIn(".storlek", html)
        start = html.index(f'data-sg-tpl="{value}"')
        template = html[start : html.index("</template>", start)]
        self.assertIn('<option value="in" selected>valde</option>', template)
        self.assertIn('<option value="not_in">valde inte</option>', template)
        self.assertIn('<option value="">Välj svar</option>', template)
        self.assertIn('<option value="felsokning">Felsökning</option>', template)
        # Gruppen står sist i väljaren.
        self.assertGreater(html.index("Svar i formulär"), html.index('label="Aktivitet"'))

    def test_save_and_the_live_count(self):
        a = self.contact()
        self.lead(a, self.entry("reparation"))
        value = f"answer:{self.page.pk}.tjanst"
        response = self.client.post(
            reverse("flamingo:app_segment_new"),
            {"namn": "Valde reparation", "r0_f": value, "r0_op": "in", "r0_v": "reparation"},
        )
        segment = Segment.objects.get(account=self.account, name="Valde reparation")
        self.assertRedirects(response, reverse("flamingo:app_segment", args=[segment.pk]))
        self.assertEqual(segment.rules, {"all": [{"f": value, "op": "in", "v": ["reparation"]}]})
        self.assertEqual(segment.cached_count, 1)
        page = self.client.get(reverse("flamingo:app_segment", args=[segment.pk]))
        html = page.content.decode()
        self.assertIn(f'<option value="{value}" selected>', html)
        self.assertIn('<option value="reparation" selected>Reparation</option>', html)
        self.assertIn(f"Valde Reparation i &quot;{LABEL}&quot; (Bilservice)", html)
        counted = self.client.post(
            reverse("flamingo:app_segment_count"),
            {"r0_f": value, "r0_op": "in", "r0_v": ["reparation", "felsokning"]},
        ).json()
        self.assertTrue(counted["ok"])
        self.assertEqual(counted["total"], 1)
        self.assertEqual(
            counted["lines"], [f'Valde Reparation eller Felsökning i "{LABEL}" (Bilservice)']
        )
        # Inget svar valt än: räknas inte, och säger det.
        counted = self.client.post(
            reverse("flamingo:app_segment_count"), {"r0_f": value, "r0_op": "in", "r0_v": ""}
        ).json()
        self.assertEqual(counted["note"], "Villkor 1 räknas inte förrän det är ifyllt.")

    def test_a_question_that_is_gone(self):
        segment = Segment.objects.create(
            account=self.account, name="Gammal fråga", rules={"all": [self.rule(q="borta")]}
        )
        url = reverse("flamingo:app_segment", args=[segment.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(segments.GONE_QUESTION_TEXT, response.content.decode())
        response = self.client.post(
            url,
            {"namn": "Gammal fråga", "r0_f": f"answer:{self.page.pk}.borta", "r0_op": "in"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(segments.GONE_QUESTION_TEXT, response.content.decode())
        # Ett alternativ som tagits bort: raden säger det.
        segment.rules = {"all": [self.rule(v=["borta"])]}
        segment.save()
        self.assertIn(
            "Det valda finns inte längre. Välj igen.", self.client.get(url).content.decode()
        )

    def test_a_foreign_page_is_400_everywhere(self):
        value = f"answer:{self.other_page.pk}.tjanst"
        data = {"namn": "Hemligt", "r0_f": value, "r0_op": "in", "r0_v": "reparation"}
        self.assertEqual(
            self.client.post(reverse("flamingo:app_segment_new"), data).status_code, 400
        )
        self.assertEqual(
            self.client.post(reverse("flamingo:app_segment_count"), data).status_code, 400
        )
        self.assertFalse(Segment.objects.filter(name="Hemligt").exists())


# ---------------------------------------------------------------------------
# Namngivna länkar
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class NamedLinkTests(AnswerFixture, TestCase):
    def post(self, **data):
        values = {
            "beskrivning": "Affisch reparation",
            "mal": "lp",
            "kampanj": str(self.quote_page.pk),
            "adress": "",
            "slug": "",
        }
        values.update(data)
        return self.client.post(NEW_LINK_URL, values)

    def test_the_select_has_a_group_per_page_with_choices(self):
        html = self.client.get(NEW_LINK_URL).content.decode()
        self.assertIn('<select class="fl-input" id="ut-ln-forval" name="forval"', html)
        self.assertIn('data-ln-preselect="ut-ln-campaign"', html)
        self.assertIn('<option value="">Inget förval</option>', html)
        self.assertIn(
            f'<optgroup label="Badrum Nacka" data-ln-campaign="{self.quote_page.pk}">', html
        )
        self.assertIn(f'<option value="tjanst.reparation">Reparation ({LABEL})</option>', html)
        self.assertIn('<option value="nar.pa-helgen">På helgen (När passar det?)</option>', html)
        # Rörjour Nacka har inget flerval och ingen grupp.
        self.assertNotIn(f'data-ln-campaign="{self.call_page.pk}"', html)
        self.assertNotIn("Hemlig", html)

    def test_no_select_without_a_page_with_choices(self):
        self.set_questions(self.page, [OLD])
        html = self.client.get(NEW_LINK_URL).content.decode()
        self.assertNotIn('name="forval"', html)

    def test_create_with_a_preselect(self):
        response = self.post(forval="tjanst.reparation")
        link = TrackedLink.objects.get(account=self.account, utskick__isnull=True)
        self.assertRedirects(response, reverse("flamingo:app_link", args=[link.pk]))
        self.assertEqual(
            link.destination, landing_page_url(self.quote_page) + "?val=tjanst.reparation"
        )
        self.assertEqual(link.campaign_id, self.quote_page.pk)
        page = self.client.get(reverse("flamingo:app_link", args=[link.pk])).content.decode()
        self.assertIn(f"Förvalt svar: Reparation ({LABEL})", page)
        self.assertIn('<option value="tjanst.reparation" selected>', page)

    def test_a_preselect_not_on_the_chosen_page_is_refused(self):
        for value in (
            "tjanst.okand",
            "storlek.liten",
            "<script>",
            "tjanst.reparation,nar.i-veckan",
        ):
            with self.subTest(value=value):
                response = self.post(forval=value)
                self.assertEqual(response.status_code, 400)
                self.assertIn(links.PRESELECT_TEXT, response.content.decode())
        response = self.post(forval="tjanst.reparation", kampanj=str(self.call_page.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn(links.PRESELECT_TEXT, response.content.decode())
        self.assertFalse(TrackedLink.objects.filter(account=self.account, slug__gt="").exists())

    def test_a_foreign_campaign_is_400(self):
        response = self.post(kampanj=str(self.other_campaign.pk), forval="tjanst.reparation")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TrackedLink.objects.filter(account=self.account, slug__gt="").exists())

    def test_an_external_address_ignores_the_preselect(self):
        self.post(
            mal="extern", adress="https://exempelror.example/boka", forval="tjanst.reparation"
        )
        link = TrackedLink.objects.get(account=self.account, utskick__isnull=True)
        self.assertEqual(link.destination, "https://exempelror.example/boka")

    def test_edit_keeps_changes_and_removes_it(self):
        self.post(forval="tjanst.reparation")
        link = TrackedLink.objects.get(account=self.account, utskick__isnull=True)
        url = reverse("flamingo:app_link", args=[link.pk])
        base = {
            "action": "save",
            "beskrivning": "Affisch reparation",
            "mal": "lp",
            "kampanj": str(self.quote_page.pk),
        }
        self.client.post(url, {**base, "forval": "nar.i-veckan"})
        link.refresh_from_db()
        self.assertEqual(link.destination, landing_page_url(self.quote_page) + "?val=nar.i-veckan")
        response = self.client.post(url, {**base, "forval": "nar.okand"})
        self.assertEqual(response.status_code, 400)
        link.refresh_from_db()
        self.assertTrue(link.destination.endswith("?val=nar.i-veckan"))
        self.client.post(url, {**base, "forval": ""})
        link.refresh_from_db()
        self.assertEqual(link.destination, landing_page_url(self.quote_page))
        self.assertNotIn("Förvalt svar", self.client.get(url).content.decode())

    def test_a_preselect_that_is_gone_says_so(self):
        link = make_named(
            self.account,
            slug="gammal",
            kind=TrackedLink.Kind.LP,
            campaign=self.quote_page,
            destination=landing_page_url(self.quote_page) + "?val=tjanst.borta",
        )
        html = self.client.get(reverse("flamingo:app_link", args=[link.pk])).content.decode()
        self.assertIn(link_views.STALE_PRESELECT_TEXT, html)
        self.assertNotIn("Förvalt svar:", html)

    def test_the_click_takes_the_preselect_to_the_page(self):
        self.post(forval="tjanst.reparation", slug="reparation")
        link = TrackedLink.objects.get(account=self.account, slug="reparation")
        client = Client(enforce_csrf_checks=True)
        response = client.get("/exempelror/reparation", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        click = Click.objects.get(link=link)
        self.assertEqual(
            query(location), {"val": "tjanst.reparation", "ut": tokens.ut_token(click.pk)}
        )
        self.assertTrue(location.startswith(landing_page_url(self.quote_page) + "?"))
        head = client.head("/exempelror/reparation", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(
            head["Location"], landing_page_url(self.quote_page) + "?val=tjanst.reparation"
        )
        # Sidan öppnas med svaret ikryssat, och de andra inte.
        parts = urlsplit(location)
        page = Client().get(f"{parts.path}?{parts.query}")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertRegex(html, r'<input[^>]*value="reparation"[^>]*\schecked')
        self.assertNotRegex(html, r'<input[^>]*value="bilservice"[^>]*\schecked')

    def test_bare_destination_keeps_the_preselect_and_the_anchor(self):
        url = landing_page_url(self.quote_page)
        link = make_named(
            self.account,
            slug="ankare",
            kind=TrackedLink.Kind.LP,
            campaign=self.quote_page,
            destination=f"{url}?val=tjanst.reparation#formular",
        )
        self.assertEqual(links.bare_destination(link), f"{url}?val=tjanst.reparation#formular")
        built = links.build_destination(link)
        self.assertTrue(built.startswith(f"{url}?val=tjanst.reparation"))
        self.assertTrue(built.endswith("#formular"))
        # Ett trasigt förval i databasen följer inte med.
        TrackedLink.objects.filter(pk=link.pk).update(destination=f"{url}?val=%3Cscript%3E")
        link.refresh_from_db()
        self.assertEqual(links.bare_destination(link), url)


# ---------------------------------------------------------------------------
# Utskickets länkar: sms och mejl
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class SlotTests(AnswerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.draft = Utskick.objects.create(
            account=self.account, name="Vårservice", sms_body="Hej", created_by=self.anna
        )
        self.url = reverse("flamingo:app_utskick_step", args=[self.draft.pk, "innehall"])

    def add(self, **data):
        values = {
            "sms_body": "Hej",
            "action": "lank",
            "lank_typ": "lp",
            "lank_nyckel": "reparation",
            "lank_kampanj": str(self.quote_page.pk),
        }
        values.update(data)
        return self.client.post(self.url, values, follow=True)

    def test_the_step_has_the_select(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('name="lank_forval"', html)
        self.assertIn('data-ln-preselect="ut-lank-kampanj"', html)
        self.assertIn(f'<option value="tjanst.reparation">Reparation ({LABEL})</option>', html)
        self.assertIn("js/flamingo-app-links.js", html)

    def test_an_sms_link_with_a_preselect(self):
        response = self.add(lank_forval="tjanst.reparation")
        self.assertIn("Länken {länk:reparation} är tillagd i texten.", response.content.decode())
        link = TrackedLink.objects.get(utskick=self.draft, key="reparation")
        url = landing_page_url(self.quote_page)
        self.assertEqual(link.destination, f"{url}?val=tjanst.reparation")
        self.assertIn(f"Förvalt svar: Reparation ({LABEL})", response.content.decode())
        click = Click.objects.create(
            account=self.account, link=link, utskick=self.draft, channel=Click.Channel.SMS
        )
        built = links.build_destination(link, None, click)
        self.assertEqual(query(built)["val"], "tjanst.reparation")
        self.assertEqual(query(built)["ut"], tokens.ut_token(click.pk))
        self.assertTrue(built.startswith(url + "?"))

    def test_an_invalid_preselect_adds_no_link(self):
        response = self.add(lank_forval="tjanst.okand")
        self.assertIn(links.PRESELECT_TEXT, response.content.decode())
        self.assertFalse(TrackedLink.objects.filter(utskick=self.draft).exists())
        response = self.add(lank_forval="tjanst.reparation", lank_kampanj=str(self.call_page.pk))
        self.assertIn(links.PRESELECT_TEXT, response.content.decode())
        self.assertFalse(TrackedLink.objects.filter(utskick=self.draft).exists())

    def test_add_link_checks_the_page(self):
        with self.assertRaises(links.LinkRefused):
            links.add_link(self.draft, key="x", campaign=self.quote_page, preselect="nar.okand")
        link = links.add_link(self.draft, key="x", campaign=self.quote_page, preselect="")
        self.assertEqual(link.destination, landing_page_url(self.quote_page))

    def test_the_email_link_keeps_a_pasted_preselect(self):
        url = landing_page_url(self.quote_page)
        pages = sending_email.own_pages(self.account)
        cases = {
            f"{url}?val=tjanst.reparation#formular": f"{url}?val=tjanst.reparation#formular",
            f"{url}?utm_source=x&val=nar.i-veckan": f"{url}?val=nar.i-veckan",
            f"{url}?val=%3Cscript%3E": url,
            url: url,
        }
        for position, (pasted, wanted) in enumerate(cases.items()):
            with self.subTest(pasted=pasted):
                spot = SimpleNamespace(url=pasted, label="Boka", block_id="b1", position=position)
                link = sending_email._tracked_link(self.draft, spot, pages)
                self.assertEqual(link.kind, TrackedLink.Kind.LP)
                self.assertEqual(link.destination, wanted)
                self.assertEqual(links.bare_destination(link), wanted)


# ---------------------------------------------------------------------------
# GDPR-utdraget
# ---------------------------------------------------------------------------


class ExportTests(AnswerFixture, TestCase):
    def test_the_choices_are_in_the_export(self):
        kontakt = self.contact()
        self.lead(
            kontakt,
            self.entry("bilservice", "reparation"),
            answers={LABEL: "Bilservice, Reparation"},
        )
        Lead.objects.create(account=self.account, contact=kontakt, name="Utan flerval")
        data = contacts.export_contact(kontakt)
        first, second = data["förfrågningar"]
        self.assertEqual(first["svar"], {LABEL: "Bilservice, Reparation"})
        self.assertEqual(first["flerval"], [{"fråga": LABEL, "svar": ["Bilservice", "Reparation"]}])
        self.assertEqual(second["flerval"], [])


# ---------------------------------------------------------------------------
# Texterna
# ---------------------------------------------------------------------------


class GuardTests(TestCase):
    def test_the_copy(self):
        texts = [
            segments.MISSING_TEXTS["answer"],
            segments.GONE_QUESTION_TEXT,
            segments.ANSWER_CHOICE_TEXT,
            links.PRESELECT_TEXT,
            link_views.STALE_PRESELECT_TEXT,
        ]
        typographic = [chr(c) for c in (0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026)]
        for text in texts:
            with self.subTest(text=text):
                self.assertNotIn(chr(0x21), text)
                self.assertFalse([ch for ch in typographic if ch in text])
                self.assertTrue(text.endswith("."))
        self.assertEqual(segment_views.ANSWER_GROUP, "Svar i formulär")
        self.assertEqual(
            dict(segment_views.ANSWER_FORM_OPS), {"in": "valde", "not_in": "valde inte"}
        )
        self.assertEqual(form_answers.PRESELECT_PARAM, "val")
        for key, parsed in (("12.tjanst", (12, "tjanst")), ("012.x", None), ("12.Tjanst", None)):
            with self.subTest(key=key):
                self.assertEqual(segments._answer_key(key), parsed)
