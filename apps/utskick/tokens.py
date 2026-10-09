"""
Signerade länkar för utskick (README E.2, H.2). S1-delen: bekräftelsemejlets
länk, Mina utskick och tack-sidans hänvisning efter en anmälan. Senare steg
lägger till sina (ut, klicklänkar i mejl, Reply-To, formulärens engångsvärden).

    preference_token(account_id, channel, value_hash)   Mina utskick, /utskick/val/<token>/
    read_preference(token) -> PreferenceRef | None
    doi_token(consent, now)                             bekräftelsemejlet, /utskick/bekrafta/<t>/
    read_doi(token, now) -> DoiRef | None               DoiRef.expired efter DOI_DAYS dagar
    thanks_token(consent_id, now)                       tack-sidan efter en anmälan (?r=)
    read_thanks(token, now) -> consent_id | None

S2 (länk-byggaren):

    ut_token(click_id) -> str                           ?ut= på landningssidan (E.2, E.4)
    read_ut(token) -> click_id | None
    form_nonce(code, now) -> str                        formulären på länkvärdarna (E.1), 2 h
    read_form_nonce(code, nonce, now) -> bool
    undo_nonce(suppression_id, now) -> str              Ångra på k.adx.se/s/ (E.5), 30 min
    read_undo(suppression_id, nonce, now) -> bool

Alla signaturer görs med UTSKICK_LINK_KEY (keys.link_digest), aldrig med
SECRET_KEY, så att länkar i redan skickade mejl håller när SECRET_KEY byts.
Varje slag har ett eget prefix i det som signeras ("pref:", "doi:",
"tack:"), så en token av ett slag duger aldrig som ett annat.

Formerna:

    Mina utskick   <konto36>.<kanal>.<hash43>.<sig16>    (hashen som base64url)
    bekräftelse    <samtycke36>.<hash43>.<dag36>.<sig16> (dag = dagar sedan 1970, UTC)
    tack           <samtycke36>.<tid36>.<sig12>

Mina utskick bär konto, kanal och adressens hash och fungerar utan någon rad
i databasen, så att länken i ett gammalt mejl håller efter retention och
efter att kontakten tagits bort. Länken ger bara det personen själv får
göra: stänga av, avregistrera sig, och be om ett nytt bekräftelsemejl (att
slå på kräver alltid en bekräftelse). Därför går den inte ut.

Bekräftelsens länk bär samtyckets id och adressens hash: byts adressen
gäller länken inte längre. Den går ut efter DOI_DAYS dagar.

S2-formerna:

    ut             <klick62>.<sig10>                    (bara [A-Za-z0-9]: leads.ut_from)
    formulär       <tid36>.<sig16>                      sig över "form:<kod>:<tid36>"
    Ångra          <tid36>.<sig16>                      sig över "undo:<spärr>:<tid36>"

ut bär bara klickets id. Vilken kampanj och vilket konto det gäller avgör
attribution.resolve mot klickets rad (samma konto som sidan, annars
ingenting). Formulärets nonce ersätter CSRF-kakan på länkvärdarna: den
binds till koden i adressen och gäller FORM_NONCE_SECONDS. Ångra binds till
spärrens id: när spärren är borttagen gäller värdet inte längre (en gång).
"""

import base64
import binascii
import hmac
from dataclasses import dataclass

from django.utils import timezone

from . import keys
from .models import CHANNELS

#: Bekräftelselänken i mejlet gäller så här många dagar (E.2).
DOI_DAYS = 14
#: Tack-sidans hänvisning gäller så här länge (sekunder).
THANKS_SECONDS = 3600
#: Formulären på länkvärdarna (E.1): sidan måste ha visats senast så här länge sedan.
FORM_NONCE_SECONDS = 2 * 3600
#: Ångra efter en avregistrering på k.adx.se/s/ (E.5).
UNDO_SECONDS = 30 * 60

_SIG_LEN = 16
_THANKS_SIG_LEN = 12
_UT_SIG_LEN = 10
_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class PreferenceRef:
    account_id: int
    channel: str
    value_hash: str


@dataclass(frozen=True)
class DoiRef:
    consent_id: int
    value_hash: str
    issued_day: int
    expired: bool = False


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _b36(number):
    number = int(number)
    if number < 0:
        raise ValueError("Bara positiva tal.")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
        if not number:
            return out


def _from_b36(text):
    if not text or len(text) > 13 or not text.isalnum() or text != text.lower():
        raise ValueError("Inget tal.")
    return int(text, 36)


def _sig(purpose, body, length=_SIG_LEN):
    return _b64(keys.link_digest(f"{purpose}:{body}"))[:length]


def _same(a, b):
    return hmac.compare_digest(a.encode("ascii", "ignore"), b.encode("ascii", "ignore"))


def _hash43(value_hash):
    """64 hextecken som 43 tecken base64url."""
    return _b64(bytes.fromhex(value_hash))


def _hash64(text):
    raw = _unb64(text)
    if len(raw) != 32:
        raise ValueError("Fel längd.")
    return raw.hex()


def _day(now=None):
    return int((now or timezone.now()).timestamp() // 86400)


# ---------------------------------------------------------------------------
# Mina utskick
# ---------------------------------------------------------------------------


def preference_token(account_id, channel, value_hash):
    """Länken till Mina utskick för en adress hos ett konto."""
    if channel not in CHANNELS or not value_hash:
        raise ValueError("Mina utskick behöver en kanal och en adress.")
    body = f"{_b36(account_id)}.{channel}.{_hash43(value_hash)}"
    return f"{body}.{_sig('pref', body)}"


def read_preference(token):
    """PreferenceRef för en äkta token, annars None."""
    parts = str(token or "").split(".")
    if len(parts) != 4:
        return None
    account36, channel, hash43, sig = parts
    if channel not in CHANNELS:
        return None
    body = f"{account36}.{channel}.{hash43}"
    try:
        if not _same(sig, _sig("pref", body)):
            return None
        return PreferenceRef(_from_b36(account36), channel, _hash64(hash43))
    except (ValueError, binascii.Error):
        return None


# ---------------------------------------------------------------------------
# Bekräftelsemejlet (dubbel opt-in)
# ---------------------------------------------------------------------------


def doi_token(consent, now=None):
    """Länken i bekräftelsemejlet för ett väntande samtycke."""
    if not consent.pk or not consent.value_hash:
        raise ValueError("Bekräftelsen behöver ett sparat samtycke med adress.")
    body = f"{_b36(consent.pk)}.{_hash43(consent.value_hash)}.{_b36(_day(now))}"
    return f"{body}.{_sig('doi', body)}"


def read_doi(token, now=None):
    """DoiRef för en äkta token (expired=True när den gått ut), annars None."""
    parts = str(token or "").split(".")
    if len(parts) != 4:
        return None
    consent36, hash43, day36, sig = parts
    body = f"{consent36}.{hash43}.{day36}"
    try:
        if not _same(sig, _sig("doi", body)):
            return None
        issued = _from_b36(day36)
        ref = DoiRef(_from_b36(consent36), _hash64(hash43), issued)
    except (ValueError, binascii.Error):
        return None
    today = _day(now)
    if issued > today + 1:
        return None
    if today - issued > DOI_DAYS:
        return DoiRef(ref.consent_id, ref.value_hash, issued, expired=True)
    return ref


# ---------------------------------------------------------------------------
# Tack-sidan efter en anmälan
# ---------------------------------------------------------------------------


def thanks_token(consent_id, now=None):
    """Hänvisningen till tack-sidan: vilket samtycke anmälan gällde, så att
    sidan kan visa den maskerade adressen. Inga personuppgifter i adressen."""
    issued = int((now or timezone.now()).timestamp())
    body = f"{_b36(consent_id)}.{_b36(issued)}"
    return f"{body}.{_sig('tack', body, _THANKS_SIG_LEN)}"


def read_thanks(token, now=None):
    """Samtyckets id för en äkta och färsk hänvisning, annars None."""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    consent36, issued36, sig = parts
    body = f"{consent36}.{issued36}"
    try:
        if not _same(sig, _sig("tack", body, _THANKS_SIG_LEN)):
            return None
        consent_id, issued = _from_b36(consent36), _from_b36(issued36)
    except ValueError:
        return None
    age = int((now or timezone.now()).timestamp()) - issued
    if age < -60 or age > THANKS_SECONDS:
        return None
    return consent_id


# ---------------------------------------------------------------------------
# S2 (länk-byggaren): ut på landningssidan, formulären på länkvärdarna, Ångra
# ---------------------------------------------------------------------------


def _b62(number):
    number = int(number)
    if number < 0:
        raise ValueError("Bara positiva tal.")
    out = ""
    while True:
        number, rest = divmod(number, 62)
        out = _B62[rest] + out
        if not number:
            return out


def _from_b62(text):
    if not text or len(text) > 12 or any(ch not in _B62 for ch in text):
        raise ValueError("Inget tal.")
    number = 0
    for ch in text:
        number = number * 62 + _B62.index(ch)
    return number


def _sig62(purpose, body, length=_UT_SIG_LEN):
    """Signaturen som bara [A-Za-z0-9] (ut får inte ha - eller _: den
    följer med i adresser och formulär och prövas av leads.ut_from)."""
    value = int.from_bytes(keys.link_digest(f"{purpose}:{body}"), "big")
    out = ""
    for _ in range(length):
        value, rest = divmod(value, 62)
        out += _B62[rest]
    return out


def ut_token(click_id):
    """Token för ?ut= på landningssidan: klickets id och en signatur."""
    body = _b62(click_id)
    return f"{body}.{_sig62('ut', body)}"


def read_ut(token):
    """Klickets id för en äkta token, annars None. Kontot prövas inte här:
    det gör attribution.resolve mot klickets rad."""
    parts = str(token or "").split(".")
    if len(parts) != 2:
        return None
    body, sig = parts
    try:
        click_id = _from_b62(body)
    except ValueError:
        return None
    if len(sig) != _UT_SIG_LEN or not _same(sig, _sig62("ut", body)):
        return None
    return click_id or None


def _timed(purpose, subject, now=None):
    issued = _b36(int((now or timezone.now()).timestamp()))
    return f"{issued}.{_sig(purpose, f'{subject}:{issued}')}"


def _read_timed(purpose, subject, value, max_age, now=None):
    parts = str(value or "").split(".")
    if len(parts) != 2:
        return False
    issued36, sig = parts
    try:
        issued = _from_b36(issued36)
    except ValueError:
        return False
    if not _same(sig, _sig(purpose, f"{subject}:{issued36}")):
        return False
    age = int((now or timezone.now()).timestamp()) - issued
    return -60 <= age <= max_age


def form_nonce(code, now=None):
    """Engångsvärdet i formulären på k.adx.se och klick.adx.se (E.1): ersätter
    CSRF-kakan, bundet till koden (eller token) i adressen."""
    return _timed("form", code, now)


def read_form_nonce(code, nonce, now=None):
    """True när nonce hör till koden och sidan visades inom FORM_NONCE_SECONDS."""
    return _read_timed("form", code, nonce, FORM_NONCE_SECONDS, now)


def undo_nonce(suppression_id, now=None):
    """Ångra-knappen efter en avregistrering (E.5), giltig UNDO_SECONDS."""
    return _timed("undo", int(suppression_id), now)


def read_undo(suppression_id, nonce, now=None):
    return _read_timed("undo", int(suppression_id), nonce, UNDO_SECONDS, now)
