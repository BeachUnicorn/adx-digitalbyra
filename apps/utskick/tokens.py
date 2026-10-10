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

S3 (foundation; adresserna på klick.adx.se bygger links.email_url och
grannarna, svarsadresserna reply_address):

    email_click_token(recipient_id, link_id)            klick.adx.se/m/<t> (E.2), 0 = testmejl
    read_email_click(token) -> EmailClickRef | None
    unsubscribe_token(account_id, value_hash)           klick.adx.se/a/<t> (E.5), går aldrig ut
    read_unsubscribe(token) -> PreferenceRef | None
    web_view_token(utskick_id, recipient_id)            klick.adx.se/w/<t> (F.4), 0 = utan mottagare
    read_web_view(token) -> WebViewRef | None
    pixel_token(recipient_id)                           klick.adx.se/o/<t>.gif (H.5)
    read_pixel(token) -> recipient_id | None
    calendar_token(utskick_id, block_id)                klick.adx.se/c/<t>.ics (F.1 element 14)
    read_calendar(token) -> CalendarRef | None
    reply_token(kind, account_id, object_id)            lokaldelen i Reply-To och mailto (E.2)
    read_reply_token(token) -> ReplyRef | None
    reply_address(kind, account_id, object_id)          s+<t>@svar.utskick.adx.se
    read_reply_address(address) -> ReplyRef | None
    reply_confirm_token(account_id, address, now)       egen svarsadress (I.9), 7 dagar
    read_reply_confirm(token, address, now) -> account_id | None

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

S3-formerna (inga rader i databasen, E.2):

    klick i mejl   <mottagare62>.<länk62>.<sig8>        (0 = testmejl utan mottagare)
    avregistrera   <konto36>.email.<hash43>.<sig16>     som Mina utskick, eget prefix
    webbversion    <utskick62>.<mottagare62>.<sig12>
    pixel          <mottagare62>.<sig8>
    kalender       <utskick62>.<block-id>.<sig10>       (block-id b_ + 12 tecken)
    svar           <slag><konto36>.<id36>x<sig10>       bara gemener och siffror
    svarsadress    <konto36>.<hash43>.<dag36>.<sig16>   egen svarsadress, 7 dagar

Avregistreringen bär konto och adressens hash precis som Mina utskick, så
att List-Unsubscribe i ett gammalt mejl fungerar efter retentionen och
efter en GDPR-borttagning; den går aldrig ut. Svarens token är lokaldelen i
s+<token>@<UTSKICK_REPLY_DOMAIN> och ryms i 64 tecken: slaget r (svar på
ett utskick, id = mottagaren), t (svar i en tråd, id = tråden) eller u
(avregistrering via mejl, id = mottagaren). Signaturen är base36 i gemener,
eftersom e-postsystem inte alltid behåller versaler i lokaldelen; x före
signaturen skiljer, och signaturens längd är fast. När mottagarraden är
borta faller hanteraren tillbaka på kontot i token och avsändarens adress
(G.3).
"""

import base64
import binascii
import hmac
from dataclasses import dataclass

from django.conf import settings
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


# ---------------------------------------------------------------------------
# S3 (foundation): e-postens länkar på klick.adx.se och svarsadresserna
# ---------------------------------------------------------------------------

#: Signaturernas längd i S3-formerna.
_CLICK_SIG_LEN = 8
_VIEW_SIG_LEN = 12
_CALENDAR_SIG_LEN = 10
_REPLY_SIG_LEN = 10
#: Svarens slag (lokaldelens första tecken, E.2 och G.3).
REPLY = "r"
THREAD = "t"
MAILTO = "u"
REPLY_KINDS = (REPLY, THREAD, MAILTO)
#: Länken som bekräftar en egen svarsadress gäller så här många dagar (I.9).
REPLY_CONFIRM_DAYS = 7
#: Formen på ett block-id i email_doc (pagebuilder.blocks.BLOCK_ID).
_BLOCK_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class EmailClickRef:
    #: None för ett testmejl (ingen mottagare): klicket räknas inte.
    recipient_id: int | None
    link_id: int


@dataclass(frozen=True)
class WebViewRef:
    utskick_id: int
    #: None när länken inte hör till en mottagare (testmejl).
    recipient_id: int | None


@dataclass(frozen=True)
class CalendarRef:
    utskick_id: int
    block_id: str


@dataclass(frozen=True)
class ReplyRef:
    #: REPLY, THREAD eller MAILTO.
    kind: str
    account_id: int
    #: Mottagarens id (REPLY, MAILTO) eller trådens (THREAD).
    object_id: int


def _sig36(purpose, body, length):
    """Signaturen som bara gemener och siffror (svarsadresserna)."""
    value = int.from_bytes(keys.link_digest(f"{purpose}:{body}"), "big")
    out = ""
    for _ in range(length):
        value, rest = divmod(value, 36)
        out += _B36[rest]
    return out


def _split62(token, count):
    """count tal i base62 och en signatur, åtskilda med punkt: (tal, sig)
    eller None."""
    parts = str(token or "").split(".")
    if len(parts) != count + 1:
        return None
    try:
        numbers = [_from_b62(part) for part in parts[:count]]
    except ValueError:
        return None
    return numbers, parts[-1], ".".join(parts[:count])


def email_click_token(recipient_id, link_id):
    """Klicklänken i ett mejl (klick.adx.se/m/<token>): mottagaren och
    länken. recipient_id 0 eller None för ett testmejl."""
    body = f"{_b62(recipient_id or 0)}.{_b62(link_id)}"
    return f"{body}.{_sig62('m', body, _CLICK_SIG_LEN)}"


def read_email_click(token):
    """EmailClickRef för en äkta token, annars None."""
    split = _split62(token, 2)
    if split is None:
        return None
    (recipient_id, link_id), sig, body = split
    if len(sig) != _CLICK_SIG_LEN or not _same(sig, _sig62("m", body, _CLICK_SIG_LEN)):
        return None
    if not link_id:
        return None
    return EmailClickRef(recipient_id or None, link_id)


def unsubscribe_token(account_id, value_hash):
    """Avregistreringen i mejlet (klick.adx.se/a/<token> och List-Unsubscribe).
    Som Mina utskick men med eget prefix: en avregistreringstoken duger
    aldrig som en annan sorts länk och tvärtom."""
    if not value_hash:
        raise ValueError("Avregistreringen behöver en adress.")
    body = f"{_b36(account_id)}.email.{_hash43(value_hash)}"
    return f"{body}.{_sig('unsub', body)}"


def read_unsubscribe(token):
    """PreferenceRef (kanal email) för en äkta token, annars None."""
    parts = str(token or "").split(".")
    if len(parts) != 4 or parts[1] != "email":
        return None
    account36, channel, hash43, sig = parts
    body = f"{account36}.{channel}.{hash43}"
    try:
        if not _same(sig, _sig("unsub", body)):
            return None
        return PreferenceRef(_from_b36(account36), channel, _hash64(hash43))
    except (ValueError, binascii.Error):
        return None


def web_view_token(utskick_id, recipient_id=None):
    """ "Visa i webbläsaren" (klick.adx.se/w/<token>)."""
    body = f"{_b62(utskick_id)}.{_b62(recipient_id or 0)}"
    return f"{body}.{_sig62('w', body, _VIEW_SIG_LEN)}"


def read_web_view(token):
    split = _split62(token, 2)
    if split is None:
        return None
    (utskick_id, recipient_id), sig, body = split
    if len(sig) != _VIEW_SIG_LEN or not _same(sig, _sig62("w", body, _VIEW_SIG_LEN)):
        return None
    if not utskick_id:
        return None
    return WebViewRef(utskick_id, recipient_id or None)


def pixel_token(recipient_id):
    """Öppningspixeln (klick.adx.se/o/<token>.gif), bara för mottagare med
    tracking_ok (H.5)."""
    body = _b62(recipient_id)
    return f"{body}.{_sig62('o', body, _CLICK_SIG_LEN)}"


def read_pixel(token):
    """Mottagarens id för en äkta token, annars None."""
    split = _split62(token, 1)
    if split is None:
        return None
    (recipient_id,), sig, body = split
    if len(sig) != _CLICK_SIG_LEN or not _same(sig, _sig62("o", body, _CLICK_SIG_LEN)):
        return None
    return recipient_id or None


def calendar_token(utskick_id, block_id):
    """ "Lägg till i kalendern" (klick.adx.se/c/<token>.ics) för ett
    händelseblock i utskicket."""
    block_id = str(block_id or "")
    if not _valid_block_id(block_id):
        raise ValueError("Kalendern behöver ett block-id.")
    body = f"{_b62(utskick_id)}.{block_id}"
    return f"{body}.{_sig62('c', body, _CALENDAR_SIG_LEN)}"


def _valid_block_id(block_id):
    return (
        len(block_id) == 14
        and block_id.startswith("b_")
        and all(ch in _BLOCK_ID_CHARS for ch in block_id[2:])
    )


def read_calendar(token):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    utskick62, block_id, sig = parts
    try:
        utskick_id = _from_b62(utskick62)
    except ValueError:
        return None
    if not utskick_id or not _valid_block_id(block_id):
        return None
    body = f"{utskick62}.{block_id}"
    if len(sig) != _CALENDAR_SIG_LEN or not _same(sig, _sig62("c", body, _CALENDAR_SIG_LEN)):
        return None
    return CalendarRef(utskick_id, block_id)


def reply_token(kind, account_id, object_id):
    """Lokaldelen efter s+ i Reply-To (REPLY, THREAD) och i mailto-länken
    för avregistrering (MAILTO): "r1a.f4x" + tio tecken signatur."""
    if kind not in REPLY_KINDS:
        raise ValueError(f"Okänt slag av svar: {kind!r}")
    body = f"{kind}{_b36(account_id)}.{_b36(object_id)}"
    return f"{body}x{_sig36('mail', body, _REPLY_SIG_LEN)}"


def read_reply_token(token):
    """ReplyRef för en äkta token (skiftläget spelar ingen roll), annars None."""
    token = str(token or "").strip().lower()
    if len(token) < _REPLY_SIG_LEN + 5 or token[-(_REPLY_SIG_LEN + 1)] != "x":
        return None
    body, sig = token[: -(_REPLY_SIG_LEN + 1)], token[-_REPLY_SIG_LEN:]
    kind = body[:1]
    if kind not in REPLY_KINDS:
        return None
    parts = body[1:].split(".")
    if len(parts) != 2:
        return None
    try:
        account_id, object_id = _from_b36(parts[0]), _from_b36(parts[1])
    except ValueError:
        return None
    if not account_id or not object_id:
        return None
    if not _same(sig, _sig36("mail", body, _REPLY_SIG_LEN)):
        return None
    return ReplyRef(kind, account_id, object_id)


def reply_domain():
    """UTSKICK_REPLY_DOMAIN i gemener (svar.utskick.adx.se)."""
    return (getattr(settings, "UTSKICK_REPLY_DOMAIN", "") or "svar.utskick.adx.se").lower()


def reply_address(kind, account_id, object_id):
    """s+<token>@svar.utskick.adx.se: Reply-To för ett utskick (REPLY) eller
    ett svar från Inkorgen (THREAD), och mailto-avregistreringen (MAILTO)."""
    return f"s+{reply_token(kind, account_id, object_id)}@{reply_domain()}"


def read_reply_address(address):
    """ReplyRef för en adress på svarsdomänen med en äkta token, annars
    None. Läses ur SES receipt.recipients, aldrig ur To-huvudet (G.3)."""
    address = str(address or "").strip().lower()
    local, at, domain = address.rpartition("@")
    if not at or domain != reply_domain() or not local.startswith("s+"):
        return None
    return read_reply_token(local[2:])


def reply_confirm_token(account_id, address, now=None):
    """Länken som bekräftar en egen svarsadress (UtskickSettings.own_reply_to,
    I.9). Bunden till adressen: byts den gäller länken inte längre."""
    value_hash = keys.value_hash("email", str(address or "").strip().lower())
    if not value_hash:
        raise ValueError("Bekräftelsen behöver en adress.")
    body = f"{_b36(account_id)}.{_hash43(value_hash)}.{_b36(_day(now))}"
    return f"{body}.{_sig('replyto', body)}"


def read_reply_confirm(token, address, now=None):
    """Kontots id när token är äkta, gäller address och är högst
    REPLY_CONFIRM_DAYS dagar gammal, annars None."""
    parts = str(token or "").split(".")
    if len(parts) != 4:
        return None
    account36, hash43, day36, sig = parts
    body = f"{account36}.{hash43}.{day36}"
    try:
        if not _same(sig, _sig("replyto", body)):
            return None
        account_id, value_hash, issued = _from_b36(account36), _hash64(hash43), _from_b36(day36)
    except (ValueError, binascii.Error):
        return None
    expected = keys.value_hash("email", str(address or "").strip().lower())
    if not _same(value_hash, expected):
        return None
    today = _day(now)
    if issued > today + 1 or today - issued > REPLY_CONFIRM_DAYS:
        return None
    return account_id
