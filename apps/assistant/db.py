"""Databasjobb från MCP-servern och OAuth-flödet.

ASGI-appen för assistenten går utanför Djangos förfrågningscykel, så
signalerna som annars stänger trasiga och gamla anslutningar körs aldrig.
När Postgres startades om (unattended-upgrades 2026-10-09) låg en död
anslutning kvar i tråden, och varje MCP-anrop fick "the connection is
closed" tills appen startades om. Därför: städa före och efter varje jobb,
precis som en vanlig förfrågan gör.
"""

from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import sync_to_async
from django.db import connections


def _tidy():
    # Som django.db.close_old_connections, men aldrig mitt i en transaktion
    # (där finns inget att städa, och testernas TestCase lever i en sådan).
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close_if_unusable_or_obsolete()


def db_sync(fn):
    """sync_to_async(fn, thread_sensitive=True) med anslutningsstädning."""

    def run(*args, **kwargs):
        _tidy()
        try:
            return fn(*args, **kwargs)
        finally:
            _tidy()

    return sync_to_async(run, thread_sensitive=True)


class TidyExecutor(ThreadPoolExecutor):
    """Händelseloopens standardpool, som städar anslutningarna efter varje jobb.

    Django ritar felsidorna (404, 403, 500) med
    sync_to_async(response_for_exception, thread_sensitive=False), alltså i
    den här poolen, och där körs aldrig request_finished. Varje tråd
    behöll därför sin anslutning för alltid (tre stod öppna i produktionen
    2026-10-10, från 404:or på /favicon.ico och /js/qrcode_twint.js), och med
    anslutningspoolen hade varje sådan tråd tagit en plats i poolen för gott.
    """

    def submit(self, fn, /, *args, **kwargs):
        def run():
            try:
                return fn(*args, **kwargs)
            finally:
                _tidy()

        return super().submit(run)


def install_tidy_executor(loop):
    """Gör TidyExecutor till loopens standardpool (en gång per loop)."""
    previous = getattr(loop, "_default_executor", None)
    if isinstance(previous, TidyExecutor):
        return
    loop.set_default_executor(TidyExecutor(thread_name_prefix="adx-default"))
    if previous is not None:
        # Hann något använda den gamla (t.ex. uvicorns adressuppslag lokalt)
        # får det jobbet bli klart; inga nya jobb hamnar där.
        previous.shutdown(wait=False)
