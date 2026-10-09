"""
Den enda vägen från utskick till apps/sms (README D.4, D5, G.2, F.8). Varje
sms från utskicken (utskick, flöden, testsändningar, svar från Inkorgen,
bekräftelser och STOPP-svar, byråns provsms) går hit och vidare till
apps.sms.service.send_for_account, så att taket, minutgränserna,
underlagen och portalen gäller som för API:t.

    send(account, *, to, body, sender, source, reference="", part_cost_hint=None)
        -> apps.sms.service.Outcome
    internal_reference(reference) -> str   "u12:345" -> "~u12:345" (som raden bär den)
    assert_not_demo(account)      DemoRefused för demokontot (lägsta lagret, D.4)
    sms_account_for(account)      kundens SmsAccount eller None
    headroom_for(source)          (headroom, global_headroom) för källan

Vad send gör: nekar demokontot (DemoRefused), prövar nycklarnas
fingeravtryck (keys.KeyMismatch, H.7), hittar kundens SmsAccount (saknas
det: Outcome med felet sms_not_enabled, som när kontot är avstängt), och
anropar send_for_account med svarsnumret tillåtet som avsändare och
marginalen i minutgränserna för massutskicken (utskick och flöden; svar,
bekräftelser och test är enstaka sms).

Vad send inte gör, och som den som anropar ansvarar för: Switchboard
(sms_enabled, nödbromsen sms_paused_until), tidsfönstret, kollisionen på
svarsnumret, spärrlistan och samtycket (sending/checks.py), och kontots
läge (utskick på, sending_blocked). Byråns provsms går före sms_enabled
med flit (J S2 steg 6).
"""

from django.conf import settings

from apps.sms import ratelimit, service
from apps.sms.models import SmsAccount, SmsMessage

from .. import keys

#: Källorna som skickar i mängd och därför lämnar plats åt kundens API.
HEADROOM_SOURCES = (SmsMessage.Source.UTSKICK, SmsMessage.Source.FLOW)
DEMO_TEXT = "Demokontot skickar aldrig."


class DemoRefused(Exception):
    """Ett försök att skicka från demokontot (README D12)."""


def assert_not_demo(account):
    if account is None or getattr(account, "is_demo", False):
        raise DemoRefused(DEMO_TEXT)


def sms_account_for(account):
    """Kundens SmsAccount (aktiverat eller inte), eller None."""
    if account is None or not account.customer_id:
        return None
    return SmsAccount.objects.filter(customer_id=account.customer_id).first()


def headroom_for(source):
    """(headroom, global_headroom) för källan: massutskicken håller sig under
    UTSKICK_SMS_ACCOUNT_PER_MINUTE och UTSKICK_SMS_GLOBAL_PER_MINUTE (C.1)."""
    if source not in HEADROOM_SOURCES:
        return 0, 0
    return ratelimit.headroom(
        getattr(settings, "UTSKICK_SMS_ACCOUNT_PER_MINUTE", 45),
        getattr(settings, "UTSKICK_SMS_GLOBAL_PER_MINUTE", 60),
    )


def internal_reference(reference):
    """Referensen som sms-raden bär: Flamingos egna börjar med "~"
    (service.INTERNAL_PREFIX), så att kundens API-referenser aldrig krockar
    med dem och en API-nyckel aldrig kan ta över en mottagares sms."""
    reference = str(reference or "")
    if not reference or reference.startswith(service.INTERNAL_PREFIX):
        return reference
    return service.INTERNAL_PREFIX + reference


def send(account, *, to, body, sender, source, reference="", part_cost_hint=None):
    """Ett sms för kontot (FlamingoAccount). Returnerar alltid ett
    service.Outcome, utom för demokontot (DemoRefused) och fel nyckel
    (keys.KeyMismatch). reference: "u<utskick>:<mottagare>" för utskick,
    "t<trådmeddelande>" för svar, "x<inkommande>" för STOPP-svar (D.5, G);
    raden får den med "~" först (internal_reference)."""
    assert_not_demo(account)
    keys.require_fingerprints()
    sms_account = sms_account_for(account)
    if sms_account is None:
        return service.fail("sms_not_enabled")
    headroom, global_headroom = headroom_for(source)
    data = {"to": to, "message": body, "from": sender}
    return service.send_for_account(
        sms_account,
        data,
        source=source,
        allow_reply_number=True,
        headroom=headroom,
        global_headroom=global_headroom,
        part_cost_hint=part_cost_hint,
        internal_reference=internal_reference(reference),
    )
