"""ADX Flamingo: flerval i landningssidans formulär (Giovanni 2026-10-10,
apps/flamingo/answers.py): schemat och äldre frågor, högst två flerval,
problemen med alternativen, FormSpec, knapparna på sidan, svaren som
krävs, svaren på förfrågan, förvalet ?val=, inkorgen, rapporten Svar i
formuläret, gränssnittet mot utskicken och AI."""

import copy
import json
import re
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.projects.models import Customer

from . import answers as form_answers
from . import pagebuilder
from .models import Campaign, Fact, FlamingoAccount, LandingPage, Lead, Service
from .pagebuilder import ai as pb_ai
from .pagebuilder import blocks, principles, registry

User = get_user_model()
PHONE = "08-000 00 00"
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
#: En äldre fråga: bara nyckel, text och sort.
OLD = {"key": "storlek", "label": "Ungefär hur stort?", "kind": "text"}
NBSP = "\xa0"


def _form_html(html):
    start = html.index('id="formular"')
    return html[start : html.index("</section>", start)]


class ChoiceFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.customer = Customer.objects.create(name="Bilverkstan AB")
        cls.anna = User.objects.create_user("anna@bil.se", email="anna@bil.se", password="x")
        cls.customer.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(customer=cls.customer, is_enabled=True)
        Fact.objects.create(
            account=cls.account, key="telefon", label="Telefon", value=PHONE, confirmed=True
        )
        cls.service = Service.objects.create(
            account=cls.account, name="Bilservice", sales_mode=Service.SALES_QUOTE
        )
        cls.page = cls.make_page([TJANST, NAR, OLD], name="Bilservice")
        cls.campaign = Campaign.objects.create(
            account=cls.account,
            service=cls.service,
            name="Bilservice Nacka",
            area="Nacka + 10 km",
            status=Campaign.STATUS_LIVE,
            landing_page=cls.page,
        )

        cls.other_customer = Customer.objects.create(name="Hemlig Bygg AB")
        cls.olle = User.objects.create_user("olle@hemlig.se", email="olle@hemlig.se", password="x")
        cls.other_customer.users.add(cls.olle)
        cls.other_account = FlamingoAccount.objects.create(
            customer=cls.other_customer, is_enabled=True
        )
        cls.other_page = LandingPage.objects.create(account=cls.other_account, name="Hemlig")

    @classmethod
    def make_page(cls, questions, *, name, account=None, published=True):
        account = account or cls.account
        hero = pagebuilder.new_block("hero", "form", account)
        form = pagebuilder.new_block("form", "questions", account)
        fields = pagebuilder.active_fields(form)
        fields["questions"] = copy.deepcopy(questions)
        pagebuilder.add_version(form, fields, pagebuilder.SOURCE_CUSTOMER, cls.anna)
        page_blocks = [hero, form]
        return LandingPage.objects.create(
            account=account,
            name=name,
            draft={"blocks": page_blocks},
            published={"blocks": copy.deepcopy(page_blocks)} if published else {},
            published_at=timezone.now() if published else None,
        )

    def setUp(self):
        super().setUp()
        cache.clear()
        for target, kwargs in (
            ("apps.assistant.llm.is_configured", {"return_value": False}),
            ("apps.assistant.llm.call", {"side_effect": AssertionError("AI ska inte anropas")}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def post_json(self, client, url, data):
        return client.post(
            url,
            data=json.dumps(data),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

    def form_block(self, page_blocks):
        return next(b for b in page_blocks if b["type"] == "form")

    def entry(self, o, *, q="tjanst", label=LABEL, labels=None, page=None, multi=True):
        names = {"bilservice": "Bilservice", "reparation": "Reparation", "felsokning": "Felsökning"}
        return {
            "page": self.page.pk if page is None else page,
            "q": q,
            "label": label,
            "multi": multi,
            "o": list(o),
            "labels": list(labels) if labels is not None else [names.get(k, k) for k in o],
        }

    def lead(self, *entries, account=None, status=Lead.STATUS_NEW, source=Lead.SOURCE_FORM, **kw):
        return Lead.objects.create(
            account=account or self.account,
            campaign=self.campaign if account is None else None,
            source=source,
            status=status,
            name=kw.pop("name", "Test"),
            choice_answers=list(entries),
            **kw,
        )

    def post(self, data, *, ip="10.0.0.7", url=None):
        base = {"name": "Anna Lind", "phone": "070-111 22 33"}
        base.update(data)
        return Client().post(url or self.campaign.landing_url, base, REMOTE_ADDR=ip)


# ---------------------------------------------------------------------------
# Schemat och äldre frågor
# ---------------------------------------------------------------------------


class SchemaTests(ChoiceFixture, TestCase):
    def test_the_kinds_and_the_new_sub_fields(self):
        questions = registry.TYPES["form"].field("questions")
        kinds = dict(questions.sub("kind").choices)
        self.assertEqual(kinds["one"], "Flerval, ett svar")
        self.assertEqual(kinds["many"], "Flerval, flera svar")
        self.assertEqual(list(kinds)[:3], ["text", "textarea", "date"])
        options = questions.sub("options")
        self.assertEqual(options.kind, registry.TEXTAREA)
        self.assertEqual(options.max_length, 500)
        self.assertEqual(options.label, "Alternativ för flerval, ett per rad")
        required = questions.sub("required")
        self.assertEqual(required.kind, registry.CHOICE)
        self.assertEqual(required.label, "Svar")
        self.assertEqual(required.choices, (("", "Valfritt"), ("required", "Krävs")))
        # Redigeraren får underfälten, men inte omit_when_absent.
        schema = registry.TYPES["form"].as_dict()
        subs = next(f for f in schema["fields"] if f["key"] == "questions")["items"]
        self.assertEqual([s["key"] for s in subs], ["key", "label", "kind", "options", "required"])
        self.assertNotIn("omit_when_absent", json.dumps(schema))

    def test_an_old_question_is_cleaned_unchanged(self):
        fields = pagebuilder.active_fields(pagebuilder.new_block("form", "questions", self.account))
        fields["questions"] = [dict(OLD), {"key": "jobbet", "label": "Jobbet", "kind": "textarea"}]
        cleaned = blocks.clean_fields(registry.TYPES["form"], fields)
        self.assertEqual(
            cleaned["questions"],
            [dict(OLD), {"key": "jobbet", "label": "Jobbet", "kind": "textarea"}],
        )
        # Mallens frågor får inte heller de nya fälten.
        template = pagebuilder.active_fields(pagebuilder.new_block("form", "booking", self.account))
        for question in template["questions"]:
            self.assertEqual(set(question), {"key", "label", "kind"})

    def test_a_saved_old_page_validates_byte_identical(self):
        page_blocks = self.make_page([OLD], name="Gammal").draft_blocks
        again = pagebuilder.validate_blocks(copy.deepcopy(page_blocks), account=self.account)
        self.assertEqual(
            json.dumps(again, sort_keys=True, ensure_ascii=False),
            json.dumps(page_blocks, sort_keys=True, ensure_ascii=False),
        )
        for version in self.form_block(again)["versions"]:
            self.assertTrue(pagebuilder.is_signed(version))

    def test_a_new_question_keeps_its_options_and_required(self):
        page_blocks = pagebuilder.validate_blocks(self.page.draft_blocks, account=self.account)
        questions = pagebuilder.active_fields(self.form_block(page_blocks))["questions"]
        self.assertEqual(questions[0], TJANST)
        self.assertEqual(questions[1], NAR)
        self.assertEqual(questions[2], OLD)

    def test_authorship_is_kept_across_two_saves_of_an_unchanged_old_form(self):
        """Redigeraren läser aldrig om blocken efter en sparning: lade servern
        till options och required i en äldre fråga skulle nästa sparning se
        en ändrad version och skriva den inloggades namn och tid på den."""
        page = self.make_page([OLD], name="Två sparningar", published=False)
        editor_copy = json.loads(json.dumps(page.draft_blocks))
        before = pagebuilder.active_version(self.form_block(page.draft_blocks))
        client = self.client_for(self.anna)
        url = reverse("flamingo:app_page_save", args=[page.pk])
        rev = page.rev
        for _ in range(2):
            response = self.post_json(client, url, {"rev": rev, "blocks": editor_copy})
            self.assertEqual(response.status_code, 200, response.content)
            rev = response.json()["rev"]
            page.refresh_from_db()
            after = pagebuilder.active_version(self.form_block(page.draft_blocks))
            self.assertEqual(after["fields"]["questions"], [OLD])
            for key in ("id", "source", "by", "at"):
                self.assertEqual(after[key], before[key], key)


# ---------------------------------------------------------------------------
# Högst två flerval
# ---------------------------------------------------------------------------


class ChoiceCountTests(ChoiceFixture, TestCase):
    def three(self):
        third = dict(NAR, key="mer", label="Något mer?")
        page_blocks = copy.deepcopy(self.page.draft_blocks)
        form = self.form_block(page_blocks)
        version = pagebuilder.active_version(form)
        version["fields"]["questions"] = [dict(TJANST), dict(NAR), third]
        return page_blocks

    def test_a_third_choice_question_saves_but_stops_the_publishing(self):
        """Inget schemafel: då skulle redigeraren sluta spara hela sidan så
        fort kunden byter Sorts svar på en tredje fråga (granskningen
        2026-10-10). Gränsen gäller ändå: den tredje ritas inte, och sidan
        går inte att publicera."""
        page_blocks = pagebuilder.validate_blocks(self.three(), account=self.account)
        page = LandingPage.objects.create(
            account=self.account, name="Tre flerval", draft={"blocks": page_blocks}
        )
        found = [
            (p.where, p.message)
            for p in pagebuilder.page_problems(page)
            if p.message == blocks.MSG_CHOICE_COUNT
        ]
        self.assertEqual(found, [("Formulär, fråga 3", blocks.MSG_CHOICE_COUNT)])
        self.assertEqual(
            blocks.MSG_CHOICE_COUNT,
            "Högst två flervalsfrågor i ett formulär. Byt Sorts svar på den här frågan.",
        )
        with self.assertRaises(pagebuilder.PageProblems):
            pagebuilder.publish_page(page, self.anna)

    def test_the_editor_saves_and_draws_three_and_explains_the_third(self):
        client = self.client_for(self.anna)
        three = self.three()
        response = self.post_json(
            client,
            reverse("flamingo:app_page_save", args=[self.page.pk]),
            {"rev": self.page.rev, "blocks": three},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.page.refresh_from_db()
        questions = pagebuilder.active_fields(self.form_block(self.page.draft_blocks))["questions"]
        self.assertEqual([q["kind"] for q in questions], ["many", "one", "one"])
        form = self.form_block(self.page.draft_blocks)
        response = self.post_json(
            client, reverse("flamingo:app_page_render_block", args=[self.page.pk]), {"block": form}
        )
        self.assertEqual(response.status_code, 200, response.content)
        html = response.json()["html"]
        self.assertIn(
            '<p class="rn-help" data-pb-field="questions">Högst två flervalsfrågor i ett '
            "formulär. Byt Sorts svar på den här frågan.</p>",
            html,
        )
        self.assertNotIn('name="q_mer"', html)
        spec = pagebuilder.form_spec(self.page.draft_blocks)
        self.assertEqual(
            [(q["visible"], q["over_limit"]) for q in spec.questions],
            [(True, False), (True, False), (False, True)],
        )

    def test_two_are_fine_and_text_kinds_do_not_count(self):
        page_blocks = copy.deepcopy(self.page.draft_blocks)
        version = pagebuilder.active_version(self.form_block(page_blocks))
        version["fields"]["questions"] = [
            dict(TJANST),
            dict(NAR),
            dict(OLD),
            {"key": "datum", "label": "Datum", "kind": "date"},
            {"key": "mer", "label": "Mer", "kind": "textarea", "options": "A\nB"},
        ]
        pagebuilder.validate_blocks(page_blocks, account=self.account)

    def test_the_principle_and_the_block_say_two(self):
        self.assertIn("två flervalsfrågor", principles.get("farre_falt").text)
        self.assertIn("högst två flervalsfrågor", registry.TYPES["form"].why)


# ---------------------------------------------------------------------------
# Problemen med alternativen
# ---------------------------------------------------------------------------


class OptionProblemTests(ChoiceFixture, TestCase):
    def problems(self, question):
        page = self.make_page([question], name=f"Problem {question['key']}", published=False)
        return [
            (p.where, p.message)
            for p in pagebuilder.page_problems(page)
            if p.where.startswith("Formulär")
        ]

    def test_too_few_options(self):
        found = self.problems(dict(TJANST, options="Bilservice"))
        self.assertIn(("Formulär, fråga 1", "Skriv minst två alternativ, ett per rad."), found)
        found = self.problems(dict(TJANST, options=""))
        self.assertIn(("Formulär, fråga 1", "Skriv minst två alternativ, ett per rad."), found)

    def test_too_many_options(self):
        words = "Ett\nTvå\nTre\nFyra\nFem\nSex\nSju\nÅtta\nNio"
        found = self.problems(dict(TJANST, options=words))
        self.assertIn(("Formulär, fråga 1", "Högst åtta alternativ (nu 9)."), found)

    def test_a_long_option(self):
        found = self.problems(dict(TJANST, options="Bilservice\n" + "a" * 61))
        self.assertIn(("Formulär, fråga 1", "Alternativ 2 är för långt: högst 60 tecken."), found)

    def test_two_options_that_become_the_same_answer(self):
        found = self.problems(dict(TJANST, options="Felsökning\nfelsokning\nReparation"))
        self.assertIn(
            (
                "Formulär, fråga 1",
                "Två alternativ blir samma svar: Felsökning och felsokning. Skriv dem olika.",
            ),
            found,
        )

    def test_numbers_in_options_are_answers_but_prices_are_checked(self):
        """Ett alternativ är besökarens svar, inte ett påstående: "1" och
        "Under 50 kvm" stoppar inte publiceringen. Ett pris eller en andel
        prövas som vanligt, och problemet står vid frågans alternativ."""
        for options in ("1\n2\n3\n4 eller fler", "Under 50 kvm\n50-100 kvm\nÖver 100 kvm"):
            with self.subTest(options=options):
                self.assertEqual(self.problems(dict(TJANST, options=options)), [])
        found = self.problems(dict(TJANST, options="Service 995 kr\nReparation\nRabatt 20 %"))
        self.assertEqual(
            found,
            [
                (
                    "Formulär, fråga 1, alternativen",
                    "Siffran 995 finns inte bland dina bekräftade uppgifter.",
                ),
                (
                    "Formulär, fråga 1, alternativen",
                    "Siffran 20 finns inte bland dina bekräftade uppgifter.",
                ),
            ],
        )
        # Löften prövas fortfarande (typografin normaliseras redan när
        # versionen sparas).
        found = self.problems(dict(TJANST, options="Inom en vecka\nSenare"))
        self.assertEqual(
            found,
            [
                (
                    "Formulär, fråga 1, alternativen",
                    "Lova inga tider, till exempel hur snabbt ni kommer eller hör av er.",
                )
            ],
        )

    def test_a_good_choice_and_a_text_question_with_options_have_no_problem(self):
        self.assertEqual(self.problems(dict(TJANST)), [])
        # Alternativen syns inte på en fråga med kort svar: inga problem,
        # inte heller siffrorna i dem.
        self.assertEqual(self.problems(dict(OLD, options="12 st\n3 st")), [])


# ---------------------------------------------------------------------------
# FormSpec
# ---------------------------------------------------------------------------


class FormSpecTests(ChoiceFixture, TestCase):
    def test_option_keys_come_from_the_text(self):
        self.assertEqual(blocks.option_key("Felsökning"), "felsokning")
        self.assertEqual(blocks.option_key("Däck & fälgar"), "dack-falgar")
        self.assertRegex(blocks.option_key("!!!"), r"^alt-[0-9a-f]{10}$")
        self.assertTrue(form_answers.KEY_RE.fullmatch(blocks.option_key("★★")))
        options = blocks.parse_options("  Bilservice \n\nReparation\nreparation\nFelsökning\n")
        self.assertEqual(
            options,
            [
                {"key": "bilservice", "label": "Bilservice"},
                {"key": "reparation", "label": "Reparation"},
                {"key": "felsokning", "label": "Felsökning"},
            ],
        )
        many = blocks.parse_options("\n".join(f"Val {chr(97 + i)}" for i in range(10)))
        self.assertEqual(len(many), 8)
        self.assertEqual(len(blocks.parse_options("x" * 80)[0]["label"]), 60)

    def test_keys_without_letters_follow_the_text_not_the_place(self):
        """Symboler och annan skrift än a-z (granskningen 2026-10-10): förut
        fick de "alt-<rad>", så att en omordning flyttade gamla svar, segment
        och ?val= till ett annat alternativ."""

        def keys(text):
            return {o["label"]: o["key"] for o in blocks.parse_options(text)}

        stars = keys("★★★\n★★\n★")
        self.assertEqual(len(set(stars.values())), 3)
        self.assertEqual(keys("★\n★★"), {"★": stars["★"], "★★": stars["★★"]})
        self.assertEqual(keys("★★\n★★★"), {"★★": stars["★★"], "★★★": stars["★★★"]})
        russian = keys("Да\nНет")
        self.assertEqual(keys("Нет\nДа"), russian)
        self.assertNotEqual(russian["Да"], russian["Нет"])
        # Gemener och blanksteg spelar ingen roll, som för a-z.
        self.assertEqual(blocks.option_key("да "), russian["Да"])
        # "Alt 2" och "+" krockar inte längre (förut båda alt-2).
        self.assertEqual(len(keys("Alt 2\n+")), 2)
        # Två lika symboler är samma svar, och kunden får veta det.
        self.assertEqual(len(blocks.parse_options("★\n★\nBra")), 2)
        self.assertIn(
            "Två alternativ blir samma svar: ★ och ★. Skriv dem olika.",
            blocks.option_problems("★\n★\nBra"),
        )

    def test_the_spec_of_the_page(self):
        spec = pagebuilder.form_spec(self.page.live_blocks)
        tjanst, nar, old = spec.questions
        self.assertEqual(tjanst["field"], "q_tjanst")
        self.assertTrue(tjanst["choice"] and tjanst["multi"] and tjanst["required"])
        self.assertTrue(tjanst["visible"])
        self.assertEqual(
            [o["key"] for o in tjanst["options"]], ["bilservice", "reparation", "felsokning"]
        )
        self.assertTrue(nar["choice"])
        self.assertFalse(nar["multi"] or nar["required"])
        self.assertEqual(old["options"], [])
        self.assertFalse(old["choice"] or old["required"])
        self.assertTrue(old["visible"])
        # Tre frågor, namn, telefon, meddelande och e-post.
        self.assertEqual(spec.fields, 7)

    def test_a_choice_with_one_option_and_a_third_choice_are_not_visible(self):
        raw = [
            dict(TJANST, options="Bilservice"),
            dict(NAR),
            dict(TJANST, key="a"),
            dict(NAR, key="b"),
        ]
        questions = blocks._questions(raw)
        self.assertEqual([q["visible"] for q in questions], [False, True, False, False])
        self.assertEqual([q["over_limit"] for q in questions], [False, False, True, True])
        spec = blocks.FormSpec(variant="questions", questions=questions)
        self.assertEqual(spec.fields, 1 + 2 + 1 + 1)


# ---------------------------------------------------------------------------
# Knapparna på sidan
# ---------------------------------------------------------------------------


class RenderTests(ChoiceFixture, TestCase):
    def test_the_choices_are_big_real_inputs_in_a_fieldset(self):
        html = Client().get(self.campaign.landing_url).content.decode()
        form = _form_html(html)
        self.assertEqual(form.count('<fieldset class="rn-field rn-choice'), 2)
        self.assertIn(f'<legend class="rn-label rn-choice__legend"><span>{LABEL}</span>', form)
        self.assertIn('<span class="rn-opt">(välj ett eller flera)</span></legend>', form)
        self.assertIn(
            'När passar det?</span> <span class="rn-opt">(valfritt)</span></legend>', form
        )
        self.assertEqual(form.count('type="checkbox" name="q_tjanst"'), 3)
        self.assertEqual(form.count('type="radio" name="q_nar"'), 2)
        self.assertIn(
            '<label class="rn-choice__opt" for="rn-q_tjanst.1">'
            '<input class="rn-choice__input" id="rn-q_tjanst.1" type="checkbox" '
            'name="q_tjanst" value="bilservice">',
            form,
        )
        self.assertIn(
            'value="felsokning"><span class="rn-choice__box"><span class="rn-choice__mark" '
            'aria-hidden="true"><svg class="rn-ic rn-choice__check"',
            form,
        )
        self.assertIn('<fieldset class="rn-field rn-choice rn-choice--many">', form)
        self.assertIn('<fieldset class="rn-field rn-choice">', form)
        ids = re.findall(r' id="(rn-q_[^"]+)"', form)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(" checked", form)
        # Flera svar prövas av servern; ett valfritt svar har inget required.
        self.assertNotIn('name="q_tjanst" value="bilservice" required', form)
        self.assertNotIn('name="q_nar" value="i-veckan" required', form)
        self.assertNotIn("style=", form)
        # Den äldre frågan är valfri, som förut.
        self.assertIn('Ungefär hur stort?</span> <span class="rn-opt">(valfritt)</span>', form)

    def test_every_id_in_the_form_is_unique(self):
        """Ett alternativ som heter Fel, och frågorna a och a-b med
        alternativen "B c" och "C", gav samma id förut (granskningen
        2026-10-10): felet pekade på knappen, och ett tryck på C valde B c."""
        page = self.make_page(
            [
                dict(NAR, key="nar", options="Fel\nRätt", required="required"),
                dict(TJANST, key="a", options="B c\nD", required=""),
                {"key": "a-b", "label": "Annat", "kind": "text", "required": "required"},
            ],
            name="Id",
        )
        Campaign.objects.filter(pk=self.campaign.pk).update(landing_page=page)
        for response in (
            Client().get(self.campaign.landing_url),
            self.post({}),
            self.post({"q_a": "b-c"}, ip="10.0.0.8"),
        ):
            form = _form_html(response.content.decode())
            ids = re.findall(r' id="([^"]+)"', form)
            self.assertEqual(len(ids), len(set(ids)), ids)
            for target in re.findall(r' for="([^"]+)"', form):
                self.assertEqual(ids.count(target), 1, target)
        form = _form_html(self.post({}, ip="10.0.0.9").content.decode())
        self.assertIn('<div id="rn-q_nar.fel"><p class="rn-error">Välj ett svar.</p></div>', form)
        self.assertIn('id="rn-q_nar.fel"', form)
        # Varje knapp och textfältet med fel pekar på sitt fel.
        self.assertIn(
            'value="fel" required aria-invalid="true" aria-describedby="rn-q_nar.fel">', form
        )
        self.assertIn(
            'name="q_a-b" type="text" maxlength="1000" value="" required aria-invalid="true" '
            'aria-describedby="rn-q_a-b.fel">',
            form,
        )
        self.assertIn('<div id="rn-q_a-b.fel"><p class="rn-error">Svara på frågan.</p></div>', form)

    def test_required_single_choice_and_required_text(self):
        page = self.make_page(
            [
                dict(NAR, required="required"),
                {
                    "key": "regnr",
                    "label": "Registreringsnummer",
                    "kind": "text",
                    "required": "required",
                },
                {"key": "dag", "label": "Önskad dag", "kind": "date", "required": "required"},
            ],
            name="Krävs",
        )
        Campaign.objects.filter(pk=self.campaign.pk).update(landing_page=page)
        form = _form_html(Client().get(self.campaign.landing_url).content.decode())
        self.assertIn("När passar det?</span></legend>", form)
        self.assertIn('name="q_nar" value="i-veckan" required>', form)
        label = re.search(r'<label class="rn-label" for="rn-q_regnr">(.*?)</label>', form, re.S)
        self.assertNotIn("valfritt", label.group(1))
        self.assertIn('name="q_regnr" type="text" maxlength="1000" value="" required>', form)
        self.assertIn('name="q_dag" type="date" value="" required', form)

    def test_an_incomplete_choice_only_shows_in_the_editor(self):
        page = self.make_page(
            [dict(TJANST, label="Vilken bil har du?", options="Volvo"), OLD], name="Halv"
        )
        Campaign.objects.filter(pk=self.campaign.pk).update(landing_page=page)
        public = _form_html(Client().get(self.campaign.landing_url).content.decode())
        self.assertNotIn("Vilken bil har du?", public)
        self.assertNotIn('name="q_tjanst"', public)
        form = self.form_block(page.draft_blocks)
        editing = str(pagebuilder.render_block_html(page, form, self.account, editing=True))
        self.assertIn('data-pb-field="questions.0.label">Vilken bil har du?</span>', editing)
        self.assertIn(
            '<p class="rn-help" data-pb-field="questions">Skriv minst två alternativ under '
            "Frågor, ett per rad.</p>",
            editing,
        )
        self.assertIn('data-pb-field="questions.1.label">Ungefär hur stort?', editing)
        # En fråga som inte syns räknas inte i Konverteringskollen.
        self.assertEqual(pagebuilder.form_spec(page.draft_blocks).fields, 5)

    def test_a_click_on_the_buttons_in_the_editor_opens_the_questions(self):
        form = self.form_block(self.page.draft_blocks)
        editing = str(pagebuilder.render_block_html(self.page, form, self.account, editing=True))
        self.assertEqual(
            editing.count('<div class="rn-choice__opts" data-pb-field="questions">'), 2
        )
        public = _form_html(Client().get(self.campaign.landing_url).content.decode())
        self.assertNotIn("data-pb-field", public)

    def test_the_stylesheet_has_tappable_chips_without_display_none_on_the_input(self):
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "static" / "css" / "flamingo-lp-ren.css").read_text(
            "utf-8"
        )
        rule = re.search(r"\.rn-choice__input\{([^}]*)\}", css).group(1)
        self.assertNotIn("display:none", rule)
        self.assertIn("opacity:0", rule)
        # Över hela knappen, inte en pixel i hörnet (fokusramen, "fyll i").
        self.assertIn("inset:0", rule)
        self.assertIn("width:100%;height:100%", rule)
        box = re.search(r"\.rn-choice__box\{([^}]*)\}", css).group(1)
        self.assertIn("min-height:44px", box)
        # Bockens plats finns redan: knappen växer inte när den väljs.
        check = re.search(r"\.rn-choice__check\{([^}]*)\}", css).group(1)
        self.assertIn("visibility:hidden", check)
        self.assertNotIn("display:none", check)
        self.assertIn(
            ".rn-choice__input:checked + .rn-choice__box .rn-choice__check{visibility:visible}",
            css,
        )
        self.assertIn(".rn-choice--many .rn-choice__mark{border-radius:5px}", css)
        self.assertIn(".rn-choice__input:focus-visible + .rn-choice__box{outline:", css)
        self.assertIn("@media (hover:hover){\n  .rn-choice__opt:hover", css)


# ---------------------------------------------------------------------------
# Postningen och svaren
# ---------------------------------------------------------------------------


class PostTests(ChoiceFixture, TestCase):
    def errors_for(self, response):
        return [
            m.strip()
            for m in re.findall(r'<p class="rn-error">([^<]*)</p>', response.content.decode())
        ]

    def test_a_required_multi_choice_needs_an_answer(self):
        response = self.post({})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Välj minst ett svar.", self.errors_for(response))
        self.assertIn(
            '<fieldset class="rn-field rn-choice rn-choice--many is-error" '
            'aria-describedby="rn-q_tjanst.fel">',
            response.content.decode(),
        )
        self.assertEqual(
            response.content.decode().count(
                'aria-invalid="true" aria-describedby="rn-q_tjanst.fel"'
            ),
            3,
        )
        self.assertFalse(Lead.objects.exists())

    def test_a_required_single_choice_needs_exactly_one(self):
        page = self.make_page([dict(NAR, required="required")], name="Ett svar")
        Campaign.objects.filter(pk=self.campaign.pk).update(landing_page=page)
        self.assertIn("Välj ett svar.", self.errors_for(self.post({})))
        response = self.post({"q_nar": ["i-veckan", "pa-helgen"]}, ip="10.0.0.8")
        self.assertIn("Välj bara ett svar.", self.errors_for(response))
        self.assertFalse(Lead.objects.exists())
        # Samma svar två gånger är ett svar.
        self.post({"q_nar": ["pa-helgen", "pa-helgen"]}, ip="10.0.0.9")
        self.assertEqual(Lead.objects.get().choice_answers[0]["o"], ["pa-helgen"])

    def test_an_unknown_option_is_refused(self):
        response = self.post({"q_tjanst": ["bilservice", "<script>"]})
        self.assertIn("Välj bland alternativen.", self.errors_for(response))
        response = self.post({"q_tjanst": "bilservice", "q_nar": "imorgon"}, ip="10.0.0.9")
        self.assertIn("Välj bland alternativen.", self.errors_for(response))
        self.assertFalse(Lead.objects.exists())

    def test_required_text_and_date(self):
        page = self.make_page(
            [
                {
                    "key": "regnr",
                    "label": "Registreringsnummer",
                    "kind": "text",
                    "required": "required",
                },
                {"key": "dag", "label": "Önskad dag", "kind": "date", "required": "required"},
            ],
            name="Text krävs",
        )
        Campaign.objects.filter(pk=self.campaign.pk).update(landing_page=page)
        errors = self.errors_for(self.post({}))
        self.assertIn("Svara på frågan.", errors)
        self.assertIn("Välj ett datum.", errors)

    def test_a_valid_multi_choice_is_stored_as_text_and_with_keys(self):
        response = self.post(
            {"q_tjanst": ["reparation", "bilservice"], "q_storlek": "Liten", "gclid": "Cj0abc"}
        )
        self.assertRedirects(
            response, reverse("flamingo_public:thanks", args=[self.campaign.page_slug])
        )
        lead = Lead.objects.get()
        self.assertEqual(
            lead.answers, {LABEL: "Bilservice, Reparation", "Ungefär hur stort?": "Liten"}
        )
        self.assertEqual(
            lead.choice_answers,
            [
                {
                    "page": self.page.pk,
                    "q": "tjanst",
                    "label": LABEL,
                    "multi": True,
                    "o": ["bilservice", "reparation"],
                    "labels": ["Bilservice", "Reparation"],
                }
            ],
        )
        self.assertEqual(self.page.pk, self.campaign.landing_page_id)
        self.assertEqual(mail.outbox, [])

    def test_a_single_choice_and_an_unanswered_optional_one(self):
        self.post({"q_tjanst": "felsokning", "q_nar": "pa-helgen"})
        lead = Lead.objects.get()
        self.assertEqual(lead.answers[LABEL], "Felsökning")
        self.assertEqual(lead.answers["När passar det?"], "På helgen")
        self.assertEqual(
            [(e["q"], e["multi"], e["o"]) for e in lead.choice_answers],
            [("tjanst", True, ["felsokning"]), ("nar", False, ["pa-helgen"])],
        )
        Lead.objects.all().delete()
        self.post({"q_tjanst": "felsokning"}, ip="10.0.0.8")
        lead = Lead.objects.get()
        self.assertEqual([e["q"] for e in lead.choice_answers], ["tjanst"])
        self.assertNotIn("När passar det?", lead.answers)

    def test_a_text_only_form_is_unchanged(self):
        page = self.make_page([OLD, {"key": "nar", "label": "När?", "kind": "date"}], name="Text")
        Campaign.objects.filter(pk=self.campaign.pk).update(landing_page=page)
        self.post({"q_storlek": "6", "q_nar": "2026-10-20"})
        lead = Lead.objects.get()
        self.assertEqual(lead.answers, {"Ungefär hur stort?": "6", "När?": "2026-10-20"})
        self.assertEqual(lead.choice_answers, [])
        # Äldre frågor är valfria, som förut.
        Lead.objects.all().delete()
        self.post({}, ip="10.0.0.8")
        self.assertEqual(Lead.objects.get().answers, {})

    def test_the_preview_creates_nothing(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(status=Campaign.STATUS_DRAFT)
        client = self.client_for(self.staff)
        response = client.post(
            self.campaign.landing_url,
            {"name": "Anna", "phone": "070-111 22 33", "q_tjanst": "bilservice"},
            REMOTE_ADDR="10.0.0.7",
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Lead.objects.exists())

    def test_clean_choices_refuses_garbage(self):
        clean = form_answers.clean_choices
        good = {"q": "tjanst", "label": LABEL, "multi": True, "o": ["a"], "labels": ["A"]}
        self.assertEqual(clean("x", 1), [])
        self.assertEqual(clean([None, "x", 3], 1), [])
        self.assertEqual(
            clean([dict(good, page=999)], 7),
            [
                {
                    "page": 7,
                    "q": "tjanst",
                    "label": LABEL,
                    "multi": True,
                    "o": ["a"],
                    "labels": ["A"],
                }
            ],
        )
        for bad in (
            dict(good, q="Tjänst!"),
            dict(good, o=["a", "b"]),
            dict(good, o=["A B"]),
            dict(good, o=["a", "a"], labels=["A", "A"]),
            dict(good, labels=[""]),
            dict(good, labels=[3]),
            dict(good, o="a"),
            dict(good, label=""),
            dict(good, o=[f"k{i}" for i in range(9)], labels=[f"K{i}" for i in range(9)]),
        ):
            self.assertEqual(clean([bad], 7), [], bad)
        # Text i alternativen blir ren text; multi bara när det är True.
        out = clean([dict(good, labels=["<b>A</b>"], multi="ja")], 7)
        self.assertEqual(out[0]["labels"], ["A"])
        self.assertIs(out[0]["multi"], False)
        # En post per fråga, högst åtta.
        many = [dict(good, q=f"q{i}") for i in range(12)] + [dict(good)]
        self.assertEqual(len(clean(many, 7)), 8)
        self.assertEqual(len(clean([good, good], 7)), 1)
        # Utan sida sparas inget: posten gick aldrig att räkna eller välja.
        self.assertEqual(clean([good], None), [])
        self.assertEqual(clean([good], True), [])


# ---------------------------------------------------------------------------
# Förvalet ?val=
# ---------------------------------------------------------------------------


class PreselectTests(ChoiceFixture, TestCase):
    def checked(self, query):
        response = Client().get(self.campaign.landing_url + query)
        self.assertEqual(response.status_code, 200)
        form = _form_html(response.content.decode())
        return re.findall(r'name="(q_[a-z]+)" value="([a-z-]+)" checked', form)

    def test_val_checks_the_options(self):
        self.assertEqual(self.checked("?val=tjanst.reparation"), [("q_tjanst", "reparation")])
        self.assertEqual(
            self.checked("?val=tjanst.reparation,tjanst.felsokning"),
            [("q_tjanst", "reparation"), ("q_tjanst", "felsokning")],
        )
        self.assertEqual(
            self.checked("?val=tjanst.bilservice&val=nar.pa-helgen"),
            [("q_tjanst", "bilservice"), ("q_nar", "pa-helgen")],
        )

    def test_unknown_and_garbage_values_are_ignored(self):
        for query in (
            "?val=okand.reparation",
            "?val=tjanst.okand",
            "?val=%3Cscript%3E",
            "?val=tjanst",
            "?val=storlek.liten",
            "?val=" + "a.b," * 200,
        ):
            self.assertEqual(self.checked(query), [], query)

    def test_the_first_valid_value_wins_for_a_single_answer(self):
        self.assertEqual(
            self.checked("?val=nar.okand,nar.pa-helgen,nar.i-veckan"), [("q_nar", "pa-helgen")]
        )

    def test_the_post_wins_over_val(self):
        response = Client().post(
            self.campaign.landing_url + "?val=tjanst.reparation",
            {"phone": "070-111 22 33", "q_tjanst": "felsokning"},
            REMOTE_ADDR="10.0.0.7",
        )
        self.assertEqual(response.status_code, 200)  # namnet saknas
        form = _form_html(response.content.decode())
        self.assertIn('value="felsokning" checked', form)
        self.assertNotIn('value="reparation" checked', form)
        self.assertFalse(Lead.objects.exists())

    def test_val_never_reaches_the_lead(self):
        Client().post(
            self.campaign.landing_url + "?val=tjanst.reparation&utm_source=sms",
            {"name": "Anna", "phone": "070-111 22 33", "q_tjanst": "bilservice"},
            REMOTE_ADDR="10.0.0.7",
        )
        lead = Lead.objects.get()
        self.assertEqual(lead.utm, {"utm_source": "sms"})
        self.assertEqual(lead.choice_answers[0]["o"], ["bilservice"])

    def test_parse_and_initial(self):
        self.assertEqual(
            form_answers.parse_preselect(["a.b,a.b", " c.d ", "x", "A.b"]), [("a", "b"), ("c", "d")]
        )
        self.assertEqual(len(form_answers.parse_preselect([",".join(["a.b"] * 40)])), 1)
        tokens = ",".join(f"q.o{i}" for i in range(30))
        self.assertEqual(len(form_answers.parse_preselect([tokens])), 16)
        self.assertEqual(form_answers.parse_preselect(["q.o" * 200]), [])
        spec = pagebuilder.form_spec(self.page.live_blocks)
        self.assertEqual(
            form_answers.initial_for(spec, ["nar.i-veckan,tjanst.bilservice,nar.pa-helgen"]),
            {"q_nar": "i-veckan", "q_tjanst": ["bilservice"]},
        )
        self.assertEqual(form_answers.initial_for(None, ["nar.i-veckan"]), {})


# ---------------------------------------------------------------------------
# Inkorgen
# ---------------------------------------------------------------------------


class InboxTests(ChoiceFixture, TestCase):
    def test_the_lead_and_the_card_show_the_answer(self):
        self.post({"q_tjanst": ["bilservice", "reparation"]})
        lead = Lead.objects.get()
        client = self.client_for(self.anna)
        detail = client.get(reverse("flamingo:app_lead", args=[lead.pk]))
        self.assertContains(detail, f"<dt>{LABEL}</dt><dd>Bilservice, Reparation</dd>", html=False)
        listing = client.get(reverse("flamingo:app_inbox"))
        self.assertContains(listing, "Bilservice · Bilservice, Reparation")

    def test_a_message_still_comes_first_on_the_card(self):
        self.post({"q_tjanst": "bilservice", "message": "Bromsarna låter."})
        listing = self.client_for(self.anna).get(reverse("flamingo:app_inbox"))
        self.assertContains(listing, "Bilservice · Bromsarna låter.")
        self.assertEqual(form_answers.card_text(SimpleNamespace(choice_answers="x")), "")


# ---------------------------------------------------------------------------
# Rapporten
# ---------------------------------------------------------------------------


class ReportTests(ChoiceFixture, TestCase):
    def url(self, page=None):
        return reverse("flamingo:app_page_answers", args=[(page or self.page).pk])

    def eight(self):
        for _ in range(5):
            self.lead(self.entry(["bilservice"]))
        self.lead(self.entry(["reparation"]))
        self.lead(
            self.entry(["reparation", "felsokning"]),
            self.entry(
                ["i-veckan"], q="nar", label="När passar det?", labels=["I veckan"], multi=False
            ),
        )
        self.lead(self.entry(["felsokning"]))
        # Räknas inte: skräp, ett annat konto, en annan källa och en annan sida.
        self.lead(self.entry(["bilservice"]), status=Lead.STATUS_JUNK)
        self.lead(self.entry(["bilservice"]), account=self.other_account)
        self.lead(self.entry(["bilservice"]), source=Lead.SOURCE_MANUAL)
        self.lead(self.entry(["bilservice"], page=self.other_page.pk))

    def test_counts_and_percentages(self):
        self.eight()
        report = form_answers.page_report(self.page)
        tjanst, nar = report
        self.assertEqual((tjanst["key"], tjanst["answered"], tjanst["multi"]), ("tjanst", 8, True))
        self.assertEqual(
            [(o["key"], o["n"], o["pct"], o["width"]) for o in tjanst["options"]],
            [("bilservice", 5, 62.5, 62), ("reparation", 2, 25.0, 25), ("felsokning", 2, 25.0, 25)],
        )
        self.assertEqual(
            [(o["key"], o["n"], o["pct"], o["width"]) for o in nar["options"]],
            [("i-veckan", 1, 100.0, 100), ("pa-helgen", 0, 0.0, 0)],
        )
        response = self.client_for(self.anna).get(self.url())
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(
            f'<span class="fl-svar__pct">62{NBSP}%</span><span class="fl-svar__n">(5 av 8)</span>',
            html,
        )
        self.assertIn("8 förfrågningar svarade", html)
        self.assertIn("1 förfrågan svarade", html)
        self.assertIn(
            "Procenten räknas av de 8 förfrågningar som svarade på frågan. Skräp räknas inte.", html
        )
        self.assertIn("Procenten räknas av den enda förfrågan som svarade på frågan.", html)
        self.assertIn("tillsammans kan det bli mer än 100 procent", html)
        self.assertIn('viewBox="0 0 100 10"', html)
        self.assertIn('width="62" height="10"', html)
        self.assertNotIn("<table", html)
        self.assertNotIn("style=", html.split('<div class="fl-svar">', 1)[1])
        self.assertIn("css/flamingo-app-svar.css?v=", html)

    def test_earlier_options_and_removed_questions(self):
        self.lead(self.entry(["bilservice"]))
        self.lead(self.entry(["dack"], labels=["Däck"]))
        self.lead(self.entry(["dack"], labels=["Däck och fälgar"]))
        self.lead(self.entry(["x"], q="gammal", label="Gammal fråga", labels=["Ja"], multi=False))
        report = form_answers.page_report(self.page)
        self.assertEqual([q["key"] for q in report], ["tjanst", "nar", "gammal"])
        tjanst = report[0]
        self.assertEqual(tjanst["answered"], 3)
        earlier = tjanst["options"][-1]
        self.assertEqual(
            (earlier["key"], earlier["label"], earlier["n"], earlier["current"]),
            ("dack", "Däck och fälgar", 2, False),
        )
        self.assertEqual(report[1]["answered"], 0)
        gone = report[2]
        self.assertEqual(
            (gone["current"], gone["label"], gone["answered"]), (False, "Gammal fråga", 1)
        )
        html = self.client_for(self.anna).get(self.url()).content.decode()
        self.assertIn(
            'Däck och fälgar <span class="fl-svar__gone">(finns inte längre i frågan)</span>', html
        )
        self.assertIn("Frågan finns inte längre på sidan.", html)
        self.assertIn("Inga svar än.", html)

    def test_the_note_on_several_answers_follows_the_answers(self):
        """En fråga som bytt från flera svar till ett: de äldre svaren kan
        ändå bli mer än 100 procent tillsammans, och det sägs."""
        self.lead(self.entry(["i-veckan", "pa-helgen"], q="nar", label="När passar det?"))
        self.lead(self.entry(["i-veckan"], q="nar", label="När passar det?", multi=False))
        nar = form_answers.page_report(self.page)[1]
        self.assertEqual((nar["multi"], nar["any_multi"]), (False, True))
        self.assertEqual([o["pct"] for o in nar["options"]], [100.0, 50.0])
        html = self.client_for(self.anna).get(self.url()).content.decode()
        self.assertIn("Flera svar gick att välja, så tillsammans kan det bli", html)

    def test_only_the_accounts_own_page_and_only_logged_in(self):
        response = self.client_for(self.anna).get(self.url(self.other_page))
        self.assertEqual(response.status_code, 404)
        response = self.client_for(self.olle).get(self.url())
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(Client().get(self.url()).status_code, 200)

    def test_the_empty_states(self):
        page = self.make_page([OLD], name="Utan flerval")
        html = self.client_for(self.anna).get(self.url(page)).content.decode()
        self.assertIn("Ingen flervalsfråga än", html)
        self.assertIn(
            'välj "Flerval, ett svar" eller "Flerval, flera svar" under Sorts svar.', html
        )
        # Ett flerval i utkastet som inte är publicerat än.
        page_blocks = page.draft_blocks
        version = pagebuilder.active_version(self.form_block(page_blocks))
        version["fields"]["questions"] = [dict(TJANST)]
        pagebuilder.save_draft(page, page_blocks, rev=page.rev)
        html = self.client_for(self.anna).get(self.url(page)).content.decode()
        self.assertIn(
            "Flervalsfrågan finns i utkastet. Publicera sidan, så kan besökarna svara.", html
        )

    def test_the_links_to_the_report(self):
        page = self.make_page([OLD], name="Utan flerval")
        html = self.client_for(self.anna).get(reverse("flamingo:app_pages")).content.decode()
        self.assertIn(
            f'href="{self.url()}">Svar<span class="fl-sr"> i formuläret på Bilservice</span>', html
        )
        self.assertNotIn(self.url(page), html)
        # Svar från förut räcker, också när frågan är borttagen.
        self.lead(self.entry(["bilservice"], page=page.pk))
        html = self.client_for(self.anna).get(reverse("flamingo:app_pages")).content.decode()
        self.assertIn(self.url(page), html)
        tab = self.client_for(self.anna).get(
            reverse("flamingo:app_campaign", args=[self.campaign.pk]) + "?flik=sidan"
        )
        self.assertContains(tab, f'href="{self.url()}">Svaren i formuläret</a>')
        self.assertEqual(form_answers.answered_page_ids(self.account), {page.pk})


# ---------------------------------------------------------------------------
# Gränssnittet mot utskicken
# ---------------------------------------------------------------------------


class InterfaceTests(ChoiceFixture, TestCase):
    def test_questions_for_page_and_account(self):
        questions = form_answers.questions_for_page(self.page)
        self.assertEqual([q.key for q in questions], ["tjanst", "nar"])
        tjanst = questions[0]
        self.assertEqual(
            (tjanst.page_id, tjanst.page_name, tjanst.label, tjanst.multi, tjanst.required),
            (self.page.pk, "Bilservice", LABEL, True, True),
        )
        self.assertEqual(
            tjanst.options,
            (
                ("bilservice", "Bilservice"),
                ("reparation", "Reparation"),
                ("felsokning", "Felsökning"),
            ),
        )
        self.assertEqual(tjanst.option_label("felsokning"), "Felsökning")
        self.assertEqual(tjanst.option_keys, ("bilservice", "reparation", "felsokning"))
        # Utkastet räknas inte för en publicerad sida.
        page = self.make_page([OLD], name="A först")
        page_blocks = page.draft_blocks
        pagebuilder.active_version(self.form_block(page_blocks))["fields"]["questions"] = [
            dict(TJANST, key="bil")
        ]
        pagebuilder.save_draft(page, page_blocks, rev=page.rev)
        self.assertEqual(form_answers.questions_for_page(page), [])
        self.make_page([dict(NAR, key="annat")], name="Hemligt", account=self.other_account)
        self.assertEqual(
            [(q.page_name, q.key) for q in form_answers.questions_for_account(self.account.pk)],
            [("Bilservice", "tjanst"), ("Bilservice", "nar")],
        )

    def test_questions_by_page_loads_only_the_accounts_pages_asked_for(self):
        empty = self.make_page([OLD], name="Utan flerval")
        self.make_page([dict(NAR, key="annat")], name="Ingen fråga")
        with self.assertNumQueries(1):
            found = form_answers.questions_by_page(
                self.account.pk, [self.page.pk, empty.pk, self.other_page.pk, 999999]
            )
        self.assertEqual(set(found), {self.page.pk, empty.pk})
        self.assertEqual([q.key for q in found[self.page.pk]], ["tjanst", "nar"])
        self.assertEqual(found[empty.pk], [])
        self.assertEqual(form_answers.questions_by_page(self.account.pk, []), {})
        self.assertEqual(form_answers.questions_by_page(self.account.pk, ["1", True]), {})

    def test_key_re_is_the_question_key(self):
        keys = ("tjanst", "a-b_c", "Tjanst", "-a", "a.b", "a" * 41)
        self.assertEqual(
            [bool(form_answers.KEY_RE.fullmatch(k)) for k in keys],
            [True, True, False, False, False, False],
        )

    def test_the_preselect_helpers(self):
        self.assertEqual(form_answers.PRESELECT_PARAM, "val")
        value = form_answers.preselect_value("tjanst", "reparation")
        self.assertEqual(value, "tjanst.reparation")
        url = form_answers.with_preselect("https://adx.se/lp/bil/", value)
        self.assertEqual(url, "https://adx.se/lp/bil/?val=tjanst.reparation")
        self.assertEqual(form_answers.preselect_of(url), value)
        url = form_answers.with_preselect(
            "https://adx.se/lp/bil/?utm_source=sms&val=nar.i-veckan#formular", value
        )
        self.assertEqual(
            url, "https://adx.se/lp/bil/?utm_source=sms&val=tjanst.reparation#formular"
        )
        self.assertEqual(
            form_answers.with_preselect(url, ""), "https://adx.se/lp/bil/?utm_source=sms#formular"
        )
        self.assertEqual(
            form_answers.with_preselect(url, "<script>"),
            "https://adx.se/lp/bil/?utm_source=sms#formular",
        )
        self.assertEqual(form_answers.preselect_of("https://adx.se/lp/bil/?val=x"), "")
        self.assertEqual(form_answers.preselect_of(None), "")
        self.assertEqual(form_answers.preselect_of("https://adx.se/?val=a.b,c.d"), "a.b")

    def test_preselect_choices_and_label(self):
        choices = form_answers.preselect_choices(self.campaign)
        self.assertEqual(choices[0], ("tjanst.bilservice", f"Bilservice ({LABEL})"))
        self.assertEqual(choices[-1], ("nar.pa-helgen", "På helgen (När passar det?)"))
        self.assertEqual(len(choices), 5)
        self.assertEqual(
            form_answers.preselect_label(self.campaign, "tjanst.reparation"),
            f"Reparation ({LABEL})",
        )
        self.assertEqual(form_answers.preselect_label(self.campaign, "tjanst.borta"), "")
        self.assertEqual(form_answers.preselect_label(self.campaign, ""), "")
        no_page = Campaign(account=self.account, service=self.service, name="Utan sida")
        self.assertEqual(form_answers.preselect_choices(no_page), [])
        self.assertEqual(form_answers.preselect_choices(None), [])

    def test_chose_q_in_postgres(self):
        a = self.lead(self.entry(["bilservice", "reparation"]))
        b = self.lead(self.entry(["felsokning"]))
        self.lead(self.entry(["reparation"], page=self.other_page.pk))
        self.lead(self.entry(["reparation"], q="nar"))
        self.lead()

        def found(*keys, page=None, q="tjanst"):
            rule = form_answers.chose_q(page or self.page.pk, q, list(keys))
            return set(Lead.objects.filter(rule).values_list("pk", flat=True))

        self.assertEqual(found("reparation"), {a.pk})
        self.assertEqual(found("reparation", "felsokning"), {a.pk, b.pk})
        self.assertEqual(found(), set())
        self.assertEqual(found("bilservice", page=self.other_page.pk), set())


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------


class AITests(ChoiceFixture, TestCase):
    def test_ai_never_rewrites_the_options(self):
        schema = pb_ai._field_schema()["form"]["fields"]
        questions = next(f for f in schema if f["key"] == "questions")
        self.assertEqual([s["key"] for s in questions["items"]], ["label"])
        fields = pagebuilder.active_fields(self.form_block(self.page.draft_blocks))
        form_type = registry.TYPES["form"]
        with self.assertRaises(pb_ai.AIError):
            pb_ai.target_for(form_type, "questions", fields, "questions.0.options")
        target = pb_ai.target_for(form_type, "questions", fields, "questions.0.label")
        self.assertEqual(target.current, LABEL)
