"""
Läckvakten för Sentry (apps/common/sentry.py).

Testerna kör den RIKTIGA SDK:n med exakt de inställningar produktionen får
(sentry.options), men med en transport som fångar kuverten i stället för
att skicka dem. Sedan letas hemligheten i allt som skulle ha lämnat
servern - inte i enskilda fält, för det var så läckorna gömde sig: i
query_string, i lokala variabler, i spårningshändelser utan något fel.
"""

import json
from unittest import mock

import sentry_sdk
from django.core import signals
from django.core.handlers.wsgi import WSGIHandler
from django.db import close_old_connections
from django.test import RequestFactory, TestCase, override_settings
from sentry_sdk.transport import Transport

from apps.common import sentry

STATUS_KEY = "STATUSNYCKEL-hemlig-9f3a7c"
OFFER_TOKEN = "bisR8MYOx6aAj80mtQpqsovGeHTkersA"
AI_CODE = "ADX-VTU4-MP8N"
EMAIL = "hemlig.person@example.com"


class Capture(Transport):
    sent = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type in ("event", "transaction"):
                Capture.sent.append((item.type, item.payload.json))


def call(path, method="GET", query="", body=None, **headers):
    """Genom WSGIHandler, inte testklienten: det är där Sentry kopplar in sig."""
    factory = RequestFactory()
    if body is not None:
        environ = factory.post(path, body).environ
    else:
        environ = factory._base_environ(PATH_INFO=path, REQUEST_METHOD=method, QUERY_STRING=query)
    environ.update(headers)
    try:
        list(WSGIHandler()(environ, lambda status, response_headers: None))
    except Exception:  # noqa: BLE001, S110 - undantaget ÄR testfallet
        pass


class SentryLeakTests(TestCase):
    def setUp(self):
        Capture.sent = []
        # Som testklienten gör: WSGIHandler stänger annars databasanslutningen
        # efter varje anrop, och TestCase lever i en enda transaktion.
        for signal in (signals.request_started, signals.request_finished):
            signal.disconnect(close_old_connections)
            self.addCleanup(signal.connect, close_old_connections)
        options = sentry.options("https://x@example.invalid/1", "test", "/tmp")  # noqa: S108
        production_sampler = options["traces_sampler"]
        # Produktionen spårar vart tionde anrop. Här: alla som ALLS får spåras.
        options["traces_sampler"] = lambda ctx: 1.0 if production_sampler(ctx) else 0.0
        sentry_sdk.init(transport=Capture, **options)
        self.addCleanup(sentry_sdk.init)  # utan dsn: avstängd igen

    def dump(self):
        sentry_sdk.flush()
        return json.dumps(Capture.sent, ensure_ascii=False)

    def kinds(self):
        sentry_sdk.flush()
        return [kind for kind, _ in Capture.sent]

    @override_settings(ADX_STATUS_KEY=STATUS_KEY)
    def test_status_key_never_leaves_even_when_the_view_crashes(self):
        with mock.patch("apps.monitor.status_endpoint._db", side_effect=RuntimeError("pang")):
            call("/status/adx/", HTTP_X_ADX_KEY=STATUS_KEY)
        self.assertIn("event", self.kinds())  # felet rapporteras ...
        self.assertNotIn(STATUS_KEY, self.dump())  # ... men utan nyckeln, var den än stod

    @override_settings(ADX_STATUS_KEY=STATUS_KEY)
    def test_machine_calls_are_not_traced_at_all(self):
        call("/status/adx/", HTTP_X_ADX_KEY=STATUS_KEY)
        call("/healthz/")
        self.assertEqual(self.kinds(), [])

    def test_offer_token_is_masked_in_traces(self):
        call(f"/offert/{OFFER_TOKEN}/")
        self.assertIn("transaction", self.kinds())
        dump = self.dump()
        self.assertNotIn(OFFER_TOKEN, dump)
        self.assertIn("/offert/[Filtered]", dump)

    def test_offer_token_is_masked_when_the_offer_view_crashes(self):
        with mock.patch(
            "apps.offers.public_views.get_object_or_404", side_effect=RuntimeError("pang")
        ):
            call(f"/offert/{OFFER_TOKEN}/")
        self.assertIn("event", self.kinds())
        self.assertNotIn(OFFER_TOKEN, self.dump())

    def test_ai_code_is_masked_in_query_string_and_header(self):
        call("/aiz/guide/", query=f"kod={AI_CODE}")
        call("/aiz/guide/", HTTP_X_ADX_CODE=AI_CODE)
        self.assertIn("transaction", self.kinds())
        self.assertNotIn(AI_CODE, self.dump())

    def test_form_bodies_and_cookies_stay_home(self):
        """
        Formulärkroppen och kakorna skickas aldrig. Lokala variabler gör det
        däremot (det är de som gör en felrapport användbar), så det som råkar
        ligga i en variabel när det smäller följer med - därför testas
        request-delen här och inte hela händelsen.
        """
        with mock.patch(
            "apps.projects.portal_views.contact_for_email", side_effect=RuntimeError("pang")
        ):
            csrf = "a" * 32  # token = kakans hemlighet, så CSRF-kontrollen släpper igenom
            call(
                "/kund/logga-in/",
                body={"email": EMAIL, "csrfmiddlewaretoken": csrf},
                HTTP_COOKIE=f"sessionid=abc123hemlig; csrftoken={csrf}",
            )
        self.assertIn("event", self.kinds())
        request = next(payload for kind, payload in Capture.sent if kind == "event")["request"]
        self.assertFalse(request.get("data"))
        self.assertNotIn(EMAIL, json.dumps(request))
        self.assertNotIn("abc123hemlig", self.dump())

    def test_contact_and_utskick_pages_are_not_traced(self):
        # Kontakternas sidor bär sökningar och namn, de publika sidorna tokens
        # (apps/utskick, README C.3): de spåras inte alls.
        call("/flamingo/app/kontakter/", query="q=anna.lindqvist")
        call("/flamingo/app/utskick/")
        call("/utskick/val/" + "a" * 40 + "/")
        call("/utskick/bekrafta/" + "b" * 40 + "/")
        call("/api/utskick/46elks/inkommande/" + "c" * 32 + "/")
        call("/lp/rorjour/besok/", method="POST")
        self.assertEqual(self.kinds(), [])

    def test_link_hosts_are_not_traced(self):
        link = {"wsgi_environ": {"PATH_INFO": "/", "HTTP_HOST": "k.adx.se"}}
        with override_settings(UTSKICK_LINK_HOSTS=["k.adx.se", "klick.adx.se"]):
            self.assertEqual(sentry.traces_sampler(link), 0.0)
            self.assertEqual(
                sentry.traces_sampler(
                    {"asgi_scope": {"path": "/a8Kf2X", "headers": [(b"host", b"klick.adx.se")]}}
                ),
                0.0,
            )
        self.assertEqual(
            sentry.traces_sampler({"wsgi_environ": {"PATH_INFO": "/", "HTTP_HOST": "adx.se"}}), 0.1
        )

    def test_scrubber_never_breaks_a_report(self):
        class Broken(dict):
            def items(self):
                raise KeyError("trasig")

        # Hellre ingen rapport än en omaskad.
        self.assertIsNone(sentry.scrub_event({"request": Broken(url="x")}))
        event = {"message": "ok", "extra": {"n": 1, "t": ("a", None)}}
        self.assertEqual(sentry.scrub_event(event), event)


class DropLocalsTests(TestCase):
    """Med en ram från utskick, sms, landningssidan eller inkorgen töms
    variablerna i VARJE ram: databasens params och formulärens data ligger i
    ramarna under (README C.3)."""

    def event(self, module):
        return {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "django.db.backends.utils",
                                    "vars": {"sql": "SELECT 1", "params": "['x']"},
                                },
                                {"module": "django.forms.forms", "vars": {"self": "<Form>"}},
                                {"module": module, "vars": {"n": "1"}},
                            ]
                        }
                    }
                ]
            }
        }

    def frames(self, event):
        return event["exception"]["values"][0]["stacktrace"]["frames"]

    def test_every_frame_loses_its_locals_under_an_utskick_frame(self):
        for module in (
            "apps.utskick.contacts",
            "apps.sms.service",
            "apps.flamingo.public_views",
            "apps.flamingo.app_views.inbox",
        ):
            with self.subTest(module=module):
                scrubbed = sentry.scrub_event(self.event(module))
                self.assertEqual([f["vars"] for f in self.frames(scrubbed)], [{}, {}, {}])

    def test_other_events_keep_their_locals(self):
        scrubbed = sentry.scrub_event(self.event("apps.projects.views"))
        self.assertEqual(self.frames(scrubbed)[2]["vars"], {"n": "1"})
        self.assertEqual(self.frames(scrubbed)[0]["vars"]["sql"], "SELECT 1")

    def test_utskick_names_are_in_the_denylist(self):
        for name in ("address", "phone", "email", "text_shown", "evidence", "params", "raw"):
            self.assertIn(name, sentry.SECRET_NAMES)


class ScrubTextTests(TestCase):
    def test_utskick_patterns(self):
        token = "eyJhIjoxfQ.abcdefghijklmnopqrstuv"
        cases = {
            f"https://adx.se/utskick/bekrafta/{token}/": "https://adx.se/utskick/bekrafta/[Filtered]/",
            f"/utskick/val/{token}/": "/utskick/val/[Filtered]/",
            "/api/utskick/46elks/inkommande/" + "A" * 32 + "/": (
                "/api/utskick/46elks/inkommande/[Filtered]/"
            ),
            "se https://k.adx.se/a8Kf2X nu": "se https://k.adx.se/[Filtered] nu",
            "https://klick.adx.se/a/abc.def": "https://klick.adx.se/[Filtered]",
            "svar till s+ab12.cd34@svar.utskick.adx.se": "svar till [Filtered]",
            "nyckel adxut_abcdefgh12345": "nyckel [Filtered]",
            "ut=AbC123.def4567890&x=1": "ut=[Filtered]&x=1",
            "/lp/rorjour/?adx=abc123": "/lp/rorjour/?adx=[Filtered]",
            "q=anna&sida=2": "q=[Filtered]&sida=2",
            "till +46701740605 i går": "till [Filtered] i går",
            "ring 070-174 06 05 nu": "ring [Filtered] nu",
            "från anna.lindqvist@exempelror.example": "från [Filtered]",
            # Inga falska träffar på det som inte är personuppgifter.
            "GET /flamingo/app/kontakter/?sida=2": "GET /flamingo/app/kontakter/?sida=2",
            "release adx@3cbdae0": "release adx@3cbdae0",
        }
        for raw, expected in cases.items():
            self.assertEqual(sentry.scrub_text(raw), expected, raw)

    def test_patterns(self):
        cases = {
            f"https://adx.se/offert/{OFFER_TOKEN}/acceptera/": (
                "https://adx.se/offert/[Filtered]/acceptera/"
            ),
            f"kod={AI_CODE}&format=json": "kod=[Filtered]&format=json",
            "format=json&key=abc123": "format=json&key=[Filtered]",
            f"Läs https://adx.se/aiz/ och använd koden {AI_CODE}": (
                "Läs https://adx.se/aiz/ och använd koden [Filtered]"
            ),
            "GET /webbutveckling/?utm_source=x": "GET /webbutveckling/?utm_source=x",
            'data-key="ADX-12"': 'data-key="ADX-12"',  # ärendenycklar är inga hemligheter
        }
        for raw, expected in cases.items():
            self.assertEqual(sentry.scrub_text(raw), expected, raw)
