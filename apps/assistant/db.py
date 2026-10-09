"""Databasjobb från MCP-servern och OAuth-flödet.

ASGI-appen för assistenten går utanför Djangos förfrågningscykel, så
signalerna som annars stänger trasiga och gamla anslutningar körs aldrig.
När Postgres startades om (unattended-upgrades 2026-10-09) låg en död
anslutning kvar i tråden, och varje MCP-anrop fick "the connection is
closed" tills appen startades om. Därför: städa före och efter varje jobb,
precis som en vanlig förfrågan gör.
"""

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
