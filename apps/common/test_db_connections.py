"""
Anslutningarna till Postgres hålls inom ett tak (config/settings/base.py,
DB_POOL): grinden för Django-förfrågningar, städningen i händelseloopens
standardpool, 404 i förfrågans tråd, trådarna som stänger sin anslutning och
modellanropen som inte håller någon medan modellen svarar.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection, connections
from django.http import Http404, HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.assistant.asgi_app import DjangoGate
from apps.assistant.db import TidyExecutor, install_tidy_executor
from apps.core.middleware import NotFoundInRequestThreadMiddleware


def _query_and_return_wrapper():
    """Öppna trådens anslutning och lämna tillbaka själva omslaget (inte
    proxyn django.db.connection, som slår upp tråden där den läses)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return connections["default"]


class TidyExecutorTests(TestCase):
    def test_a_plain_pool_keeps_the_thread_connection_open(self):
        # Så såg det ut före: felsidornas trådar höll sin anslutning.
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            wrapper = executor.submit(_query_and_return_wrapper).result()
            self.assertIsNotNone(wrapper.connection)
            executor.submit(wrapper.close).result()
        finally:
            executor.shutdown(wait=True)

    def test_the_connection_is_closed_after_each_job(self):
        executor = TidyExecutor(max_workers=1)
        try:
            wrapper = executor.submit(_query_and_return_wrapper).result()
            self.assertIsNone(wrapper.connection)
        finally:
            executor.shutdown(wait=True)

    def test_the_loop_default_executor_tidies(self):
        """Djangos felsidor går via sync_to_async(thread_sensitive=False)."""

        async def main():
            loop = asyncio.get_running_loop()
            install_tidy_executor(loop)
            install_tidy_executor(loop)  # en gång räcker, andra gången är ingen ändring
            via_loop = await loop.run_in_executor(None, _query_and_return_wrapper)
            via_asgiref = await sync_to_async(_query_and_return_wrapper, thread_sensitive=False)()
            return via_loop, via_asgiref

        for wrapper in asyncio.run(main()):
            self.assertIsNone(wrapper.connection)


class DjangoGateTests(SimpleTestCase):
    def peak(self, limit, requests=12):
        gate = DjangoGate(limit)
        state = {"running": 0, "peak": 0}

        async def app(scope, receive, send):
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.01)
            state["running"] -= 1

        async def main():
            await asyncio.gather(*(gate(app, {}, None, None) for _ in range(requests)))

        asyncio.run(main())
        return state["peak"]

    def test_at_most_limit_at_a_time(self):
        self.assertEqual(self.peak(3), 3)

    def test_zero_means_no_gate(self):
        self.assertEqual(self.peak(0, requests=8), 8)

    def test_one_gate_serves_several_loops(self):
        # gunicorn har en loop per worker; testerna en ny loop per asyncio.run.
        gate = DjangoGate(2)

        async def app(scope, receive, send):
            await asyncio.sleep(0)

        async def main():
            await asyncio.gather(*(gate(app, {}, None, None) for _ in range(5)))

        asyncio.run(main())
        asyncio.run(main())

    def test_the_setting_is_below_the_pool(self):
        self.assertGreater(settings.WEB_CONCURRENT_REQUESTS, 0)
        self.assertLess(settings.WEB_CONCURRENT_REQUESTS, settings.DB_POOL_MAX_SIZE)

    def test_the_pool_is_twice_the_gate(self):
        # En förfrågan kan behöva en andra anslutning medan den håller sin
        # första; med bara 2 över räckte två bakgrundstrådar för att alla
        # förfrågningar skulle vänta ut poolen (lasttestet 2026-10-10).
        self.assertGreaterEqual(settings.DB_POOL_MAX_SIZE, 2 * settings.WEB_CONCURRENT_REQUESTS)


class RouterTests(TestCase):
    """Django-förfrågningarna går genom grinden, och loopen får TidyExecutor."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.assistant.asgi_app import build_application

        cls.app = staticmethod(build_application())

    def test_a_django_request_installs_the_tidy_executor(self):
        sent = []
        messages = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            # Som en riktig server: kroppen först, sedan väntar Django på
            # att klienten kopplar ner (och avbryter den väntan själv).
            if messages:
                return messages.pop(0)
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/healthz/",
            "raw_path": b"/healthz/",
            "query_string": b"",
            "headers": [(b"host", b"adx.se"), (b"x-forwarded-proto", b"https")],
            "scheme": "https",
            "server": ("adx.se", 443),
            "client": ("127.0.0.1", 1),
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "root_path": "",
        }

        async def main():
            await self.app(scope, receive, send)
            return getattr(asyncio.get_running_loop(), "_default_executor", None)

        executor = asyncio.run(main())
        self.assertEqual(sent[0]["status"], 200)
        self.assertIsInstance(executor, TidyExecutor)


class NotFoundInRequestThreadTests(SimpleTestCase):
    def test_it_is_the_innermost_middleware(self):
        self.assertEqual(
            settings.MIDDLEWARE[-1], "apps.core.middleware.NotFoundInRequestThreadMiddleware"
        )

    def test_404_is_answered_here_and_other_errors_go_on(self):
        middleware = NotFoundInRequestThreadMiddleware(lambda request: HttpResponse())
        request = RequestFactory().get("/wp-login.php")
        # Sonderingen ger 404-sidans textrad utan databas (apps/core/errors.py).
        response = middleware.process_exception(request, Http404())
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(middleware.process_exception(request, RuntimeError("pang")))


class EmailThreadTests(TestCase):
    def test_the_send_thread_closes_its_connection(self):
        from apps.inquiries import emails

        seen = {}
        started = []
        real_thread = threading.Thread

        def target():
            seen["wrapper"] = _query_and_return_wrapper()

        def capture(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            started.append(thread)
            return thread

        with mock.patch.object(emails.threading, "Thread", side_effect=capture):
            emails._send_in_thread(target)
        started[0].join(10)
        self.assertIsNone(seen["wrapper"].connection)


class PoolSettingsTests(SimpleTestCase):
    """Poolen följer ADX_DB_POOL (config/asgi.py sätter den för webben)."""

    def settings_with(self, **env):
        code = (
            "import json\n"
            "from config.settings import base\n"
            "db = base.DATABASES['default']\n"
            "print(json.dumps({'engine': db['ENGINE'], 'pool': db.get('OPTIONS', {}).get('pool'),"
            " 'max_age': db.get('CONN_MAX_AGE', 0), 'checks': db.get('CONN_HEALTH_CHECKS')}))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "SENTRY_DSN": "", **env},
            cwd=settings.BASE_DIR,
            timeout=60,
        )
        return result

    def test_pool_only_when_asked(self):
        off = self.settings_with(ADX_DB_POOL="0")
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertIsNone(json.loads(off.stdout)["pool"])

        on = json.loads(self.settings_with(ADX_DB_POOL="1").stdout)
        if on["engine"] != "django.db.backends.postgresql":
            self.skipTest("poolen finns bara för Postgres")
        self.assertEqual(
            on["pool"], {"min_size": 2, "max_size": 12, "timeout": 30, "max_idle": 300}
        )
        self.assertEqual(on["max_age"], 0)
        self.assertTrue(on["checks"])

    def test_the_gate_must_stay_below_the_pool(self):
        result = self.settings_with(
            ADX_DB_POOL="1", ADX_DB_POOL_MAX_SIZE="6", ADX_WEB_CONCURRENT_REQUESTS="6"
        )
        engine_check = self.settings_with(ADX_DB_POOL="0")
        if json.loads(engine_check.stdout)["engine"] != "django.db.backends.postgresql":
            self.skipTest("poolen finns bara för Postgres")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ADX_WEB_CONCURRENT_REQUESTS", result.stderr)


#: Moduler som startar trådar, och hur trådarna lämnar tillbaka sin
#: anslutning. Med poolen tar en tråd som öppnar en anslutning och aldrig
#: stänger den en plats för gott (config/settings/base.py, DB_POOL): sex
#: sådana i en worker och sajten står still tills tjänsten startas om.
#: En ny modul med trådar fäller testet nedan tills den står här, med
#: stängningen som måste finnas i filen eller None och ett skäl.
THREAD_MODULES = {
    "apps/assistant/db.py": "_tidy()",
    "apps/assistant/tasks.py": "connection.close()",
    "apps/inquiries/emails.py": "connections.close_all()",
    "apps/utskick/sending/kick.py": "connection.close()",
    # Sidhämtningen rör inte databasen; modelltråden (_call_model) stänger.
    "apps/flamingo/scan.py": "connection.close()",
    "apps/flamingo/pagebuilder/ai.py": "connection.close()",
    # Trådarna hämtar bara filer; raderna sparas i förfrågans tråd.
    "apps/flamingo/media.py": None,
    # Länkkollen frågar bara nätet; cachen ligger i minnet.
    "apps/utskick/links.py": None,
    # Vakthunden stänger en nätverksanslutning, ingen databas.
    "apps/tools/analyzer.py": None,
}

_STARTS_THREADS = re.compile(r"threading\.(Thread|Timer)\(|ThreadPoolExecutor\(|_thread\.start_new")


class ThreadModulesTests(SimpleTestCase):
    def thread_modules(self):
        root = Path(settings.BASE_DIR)
        found = set()
        for folder in ("apps", "config"):
            for path in (root / folder).rglob("*.py"):
                if path.name.startswith("test") or "tests" in path.parts:
                    continue
                if _STARTS_THREADS.search(path.read_text(encoding="utf-8")):
                    found.add(path.relative_to(root).as_posix())
        return found

    def test_every_module_with_threads_is_listed(self):
        unknown = sorted(self.thread_modules() - THREAD_MODULES.keys())
        self.assertEqual(
            unknown,
            [],
            "Nya trådar: stäng trådens databasanslutning i finally och för in "
            "modulen i THREAD_MODULES (apps/common/test_db_connections.py).",
        )

    def test_listed_modules_still_close(self):
        root = Path(settings.BASE_DIR)
        for module, marker in THREAD_MODULES.items():
            if marker is None:
                continue
            with self.subTest(module=module):
                self.assertIn(marker, (root / module).read_text(encoding="utf-8"))


class ModelCallConnectionTests(TestCase):
    """Modellanropet håller ingen anslutning medan modellen svarar
    (apps/assistant/llm.py, _release_connection)."""

    def fake_client(self, seen):
        def create(**kwargs):
            seen["open_during_call"] = connections["default"].connection is not None
            usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0)
            return SimpleNamespace(usage=usage, stop_reason="end_turn", content=[])

        return SimpleNamespace(messages=SimpleNamespace(create=create))

    def test_a_thread_holds_no_connection_while_the_model_answers(self):
        from apps.assistant import llm
        from apps.assistant.models import AICall

        seen = {}

        def run():
            try:
                llm.call(system="s", messages=[], tools=[], user=None)
                # Nästa fråga i tråden lånar en ny anslutning.
                seen["after"] = AICall.objects.count()
            finally:
                connection.close()

        # AICall-raden skulle annars sparas utanför testets transaktion.
        with (
            mock.patch.object(llm, "client", return_value=self.fake_client(seen)),
            mock.patch.object(AICall.objects, "create") as create,
        ):
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(30)
        self.assertFalse(seen["open_during_call"])
        self.assertEqual(seen["after"], 0)
        self.assertTrue(create.call_args.kwargs["ok"])

    def test_inside_a_transaction_the_connection_stays(self):
        from apps.assistant import llm

        seen = {}
        with mock.patch.object(llm, "client", return_value=self.fake_client(seen)):
            llm.call(system="s", messages=[], tools=[], user=None)
        self.assertTrue(seen["open_during_call"])

    def test_waiting_callers_get_a_short_client_timeout(self):
        from apps.assistant import llm

        with mock.patch("anthropic.AnthropicBedrock") as bedrock:
            llm.client(timeout=llm.REQUEST_TIMEOUT, max_retries=llm.REQUEST_RETRIES)
            llm.client()
        short, default = bedrock.call_args_list
        self.assertEqual(short.kwargs["timeout"], 60.0)
        self.assertEqual(short.kwargs["max_retries"], 1)
        self.assertNotIn("timeout", default.kwargs)
        self.assertNotIn("max_retries", default.kwargs)

    def test_the_flamingo_callers_pass_it(self):
        root = Path(settings.BASE_DIR)
        for module in (
            "apps/flamingo/scan.py",
            "apps/flamingo/pagebuilder/ai.py",
            "apps/flamingo/generator.py",
        ):
            with self.subTest(module=module):
                text = (root / module).read_text(encoding="utf-8")
                self.assertIn("timeout=llm.REQUEST_TIMEOUT", text)
                self.assertIn("max_retries=llm.REQUEST_RETRIES", text)
