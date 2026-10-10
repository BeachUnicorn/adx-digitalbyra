"""
Minne per förfrågan för sajtens återkommande uppslag.

En publik sida ställde 105-153 frågor (Sentry ADX-DIGITALBYRA-8, F, G, H och
J): varje menylänk slog upp startsidan på nytt, länkarna i blocken hämtade
sina sidor en i taget och inställningarna lästes på fem ställen. Under
skannerskuren 2026-10-09 höll varje sådan förfrågan sin Postgres-anslutning
hela tiden, och anslutningarna tog slut.

memo(nyckel, beräkna) räknar fram ett värde en gång per förfrågan. Minnet
finns bara inne i en GET- eller HEAD-förfrågan (RequestMemoMiddleware), där
inget av det som minns ändras. Sparas eller raderas ändå en rad mitt i en
sådan förfrågan glöms allt (signalerna nedan). Utanför en förfrågan (cron,
MCP, skal, tester utan klient) och i POST räknas allt fram varje gång,
precis som förut.

Minnet följer förfrågan via en ContextVar, så det når också vyns tråd,
felsidans tråd och asgirefs hopp mellan dem. Trådar som vyn själv startar
ser det inte.
"""

from contextvars import ContextVar

from asgiref.sync import iscoroutinefunction
from django.db.models.signals import post_delete, post_save
from django.utils.decorators import sync_and_async_middleware

_store = ContextVar("adx_request_memo", default=None)

#: Förfrågningar som inte ändrar något: där får uppslagen minnas.
READ_ONLY_METHODS = frozenset({"GET", "HEAD"})


def active():
    """True inne i en GET- eller HEAD-förfrågan."""
    return _store.get() is not None


def memo(key, compute):
    """compute() en gång per förfrågan; utanför en förfrågan varje gång."""
    store = _store.get()
    if store is None:
        return compute()
    try:
        return store[key]
    except KeyError:
        value = store[key] = compute()
        return value


def forget(**kwargs):
    """Glöm allt i den här förfrågan. Mottagare för post_save och post_delete."""
    store = _store.get()
    if store is not None:
        store.clear()


post_save.connect(forget, dispatch_uid="request_memo_forget_on_save")
post_delete.connect(forget, dispatch_uid="request_memo_forget_on_delete")


@sync_and_async_middleware
def request_memo_middleware(get_response):
    """Öppnar minnet för GET och HEAD och stänger det när svaret är klart."""

    if iscoroutinefunction(get_response):

        async def middleware(request):
            if request.method not in READ_ONLY_METHODS:
                return await get_response(request)
            token = _store.set({})
            try:
                return await get_response(request)
            finally:
                _store.reset(token)

    else:

        def middleware(request):
            if request.method not in READ_ONLY_METHODS:
                return get_response(request)
            token = _store.set({})
            try:
                return get_response(request)
            finally:
                _store.reset(token)

    return middleware
