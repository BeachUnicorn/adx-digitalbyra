"""
Svar på mejl via SES inbound i eu-west-1 (README G.3, D8, E.2, H.6). Byggt
av inkorg-byggaren (S3, svar och avregistrering).

MX svar.utskick.adx.se -> inbound-smtp.eu-west-1.amazonaws.com; regeln
skannar spam och virus och lägger mejlet i UTSKICK_SES_INBOUND_BUCKET under
in/<SES-id> med en notis till ämnet adx-utskick-inbound -> SQS. queues.poll
ger notisen hit.

    receive(notification, now=None) -> InboundMessage | None
        i köns transaktion, efter EventReceipt(f"in:{mail.messageId}"):
        1. hinken: receipt.action är S3, bucketName == UTSKICK_SES_INBOUND_BUCKET
           och nyckeln är in/<id>; annars None (släpps ur kön och loggas)
        2. token ur receipt.recipients (aldrig To-huvudet), plustaggen på
           svarsdomänen (tokens.read_reply_address); okänd, felsignerad eller ett
           konto som inte finns: ignored, och S3-objektet tas bort utan att läsas
        3. token u (mailto i List-Unsubscribe): avregistreringen direkt, utan att
           läsa mejlet: (konto, hashen av mottagarens adress, eller av From när
           mottagarraden är borta), källa list_unsub
        4. virus FAIL: spam, objektet bort; spam FAIL: spam (bara räknat)
        5. gränserna: PER_REF_HOUR per token och PER_HOUR för hela ADX
           (Counter "inbound_mail"); över dem counted, objektet bort, byrån larmas
        6. annars InboundMessage(status pending, meta s3_key, ref, verdicts)
    process_pending(now, deadline) -> dict
        efter commit, högst PER_TICK per anrop: s3:GetObject med rollen (bara den
        fasta hinken), högst MAX_BYTES (större: bara ämnet och TOO_BIG_TEXT),
        tolkat med mime.parse; autosvar bort ur Inkorgen (status autoreply, kvar
        på det inkommande med mottagaren); text/plain före HTML (nh3 utan taggar);
        citerad historik bort (strip_quotes); bilagor bara namn och storlek; tråd
        och förfrågan som för sms (threads.email_thread_for, add_inbound), From
        prövad mot mottagarens adress ("från en annan adress"); objektet tas bort
        efteråt
    pending_exists() -> bool             queues.poll_due
    sweep_bucket(now=None) -> dict       utskick_daily: in/-objekt äldre än 30 minuter
    is_autoreply(message) -> bool        Auto-Submitted, X-Autoreply, X-Autorespond,
                                         Precedence bulk|junk|auto_reply|list,
                                         multipart/report, ämnen som börjar "Autosvar",
                                         "Automatiskt svar", "Frånvaro", "Out of office",
                                         "Automatic reply"
    strip_quotes(text) -> str            från första raden som börjar med >, "Den ...
                                         skrev:", "On ... wrote:", "-----Original
                                         Message-----" eller ett "Från:"-block

Ett mejlsvar skapar ingen kontakt: tråden får kontakten mottagaren hade, eller
kontakten som redan har adressen. Inget mejl skickas härifrån, och aldrig ett
svar på ett mejl (ingen slinga med autosvar). Inga adresser, ämnen eller
texter i loggarna, bara pk och antal. Texten och ämnet töms på
InboundMessage när svaret ligger i tråden.

Vakten i test_s1_guards (ModuleGuardTests) förbjuder varje import av
standardbibliotekets email.* utanför email/mime.py: mejlen tolkas med
mime.parse, och resten görs med EmailMessage-objektets egna metoder.
"""

import html as html_lib
import json
import logging
import re
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .. import alerts, limits, normalize, tokens
from ..models import CHANNEL_EMAIL, InboundMessage, ThreadMessage
from . import queues

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
PER_TICK = 20
PER_HOUR = 500
TOO_BIG_TEXT = "Svaret var för stort för att visas här."
ATTACHMENTS_TEXT = "Bilagor sparas inte. Be avsändaren skicka dem till din vanliga adress."
EMPTY_TEXT = "Mejlet var tomt."

#: Prefixet i hinken (receipt-regeln adx-utskick-svar, server/aws-utskick-s3.sh).
PREFIX = "in/"
#: Nyckeln är prefixet och SES-id:t; inget annat läses eller tas bort.
KEY_RE = re.compile(r"in/[A-Za-z0-9._\-]{1,200}")
#: SES mail.messageId.
ID_RE = re.compile(r"[A-Za-z0-9._\-]{1,120}")
#: Samma token (en mottagare, en tråd) får högst så här många mejl i timmen
#: innan resten bara räknas, så att en avsändare inte tar ADX hela timgräns.
PER_REF_HOUR = 20
#: Nedladdningen misslyckas så här många gånger innan raden ges upp.
MAX_ATTEMPTS = 5
#: Högst så här många bilagor listas på meddelandet.
MAX_ATTACHMENTS = 20
#: Svepet: objekt äldre än så här utan rad, högst så här många per körning.
SWEEP_AGE = timedelta(minutes=30)
SWEEP_MAX = 200
SWEEP_PAGES = 5
SWEEP_SECONDS = 60
#: Svepet läser bara huvudet av ett föräldralöst objekt (token och bedömningar).
HEAD_BYTES = 64 * 1024
#: pg_advisory_xact_lock för en adress hos ett konto i taget, bredvid
#: limits.TICK_LOCK (0x5554), ADX_MAIL_LOCK (0x5555), CONTACT_LIMIT_LOCK
#: (0x5556) och routing.INBOUND_LOCK (0x5557).
INBOUND_MAIL_LOCK = 0x5558 << 32

#: Ämnen som är autosvar (G.3 punkt 5), i gemener.
AUTO_SUBJECTS = ("autosvar", "automatiskt svar", "frånvaro", "out of office", "automatic reply")
AUTO_PRECEDENCE = frozenset({"bulk", "junk", "auto_reply", "list"})

#: Statusar där objektet i hinken är klart och tas bort.
FINAL = (
    InboundMessage.Status.ROUTED,
    InboundMessage.Status.STOP,
    InboundMessage.Status.AUTOREPLY,
    InboundMessage.Status.SPAM,
    InboundMessage.Status.IGNORED,
    InboundMessage.Status.COUNTED,
)


# ---------------------------------------------------------------------------
# Notisen
# ---------------------------------------------------------------------------


def _as_dict(notification):
    """Notisen som dict. SQS-kroppen är SES-notisen (RawMessageDelivery); ett
    SNS-kuvert packas upp om det ändå kommer ett."""
    data = notification
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    if not isinstance(data, dict):
        return {}
    if data.get("Type") == "Notification" and isinstance(data.get("Message"), str):
        return _as_dict(data["Message"])
    return data


def bucket():
    """Den fasta hinken (UTSKICK_SES_INBOUND_BUCKET), "" när inkommande är av."""
    return (getattr(settings, "UTSKICK_SES_INBOUND_BUCKET", "") or "").strip()


def _pinned_key(data):
    """Objektets nyckel när notisen kommer från vår S3-åtgärd i vår hink,
    annars None (G.3 punkt 1)."""
    action = ((data.get("receipt") or {}).get("action")) or {}
    if str(action.get("type") or "").upper() != "S3":
        return None
    expected = bucket()
    if not expected or str(action.get("bucketName") or "") != expected:
        return None
    key = str(action.get("objectKey") or "")
    if not KEY_RE.fullmatch(key):
        return None
    return key


def _verdicts(receipt):
    out = {}
    for name in ("spam", "virus", "spf", "dkim", "dmarc"):
        status = str(((receipt or {}).get(f"{name}Verdict") or {}).get("status") or "")
        if status:
            out[name] = status.upper()[:20]
    return out


def from_verified(verdicts):
    """Går From att lita på? SPF eller DKIM godkända och DMARC inte
    underkänd (receipt:ens bedömningar). Prövas bara när mottagarraden eller
    tråden saknas, för då står adressen bara i From, som vem som helst kan
    skriva."""
    verdicts = verdicts or {}
    if verdicts.get("dmarc") == "FAIL":
        return False
    return verdicts.get("dkim") == "PASS" or verdicts.get("spf") == "PASS"


def token_from(addresses):
    """(ReplyRef, adressen) för den första mottagaren med en äkta token på
    svarsdomänen, annars (None, "")."""
    if isinstance(addresses, str):
        addresses = [addresses]
    for address in addresses or []:
        ref = tokens.read_reply_address(address)
        if ref is not None:
            return ref, str(address).strip().lower()[:254]
    return None, ""


_ANGLE_RE = re.compile(r"<\s*([^<>\s]+@[^<>\s]+?)\s*>")
_BARE_RE = re.compile(r"([^\s<>\"',;:()\[\]]+@[^\s<>\"',;:()\[\]]+)")
#: Ett huvudvärde kortas till en rads längd (RFC 5322) innan uttrycken ovan
#: körs: deras tid växer med kvadraten på längden, och värdet kommer utifrån.
HEADER_CHARS = 998


def address_in(value):
    """Adressen i ett huvudvärde ("Anna Lind <anna@exempel.se>"), normaliserad
    (normalize.email), eller "" om ingen giltig adress finns."""
    text = str(value or "")[:HEADER_CHARS]
    match = _ANGLE_RE.search(text) or _BARE_RE.search(text)
    if not match:
        return ""
    try:
        return normalize.email(match.group(1).strip().strip("."))
    except normalize.InvalidValue:
        return ""


def _from_of(mail):
    common = (mail or {}).get("commonHeaders") or {}
    senders = common.get("from") or []
    if isinstance(senders, str):
        senders = [senders]
    for sender in senders[:5]:
        found = address_in(sender)
        if found:
            return found
    return address_in((mail or {}).get("source"))


def _one_line(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _message_id(value):
    """Message-ID som "<...>", annars "" (bara för In-Reply-To, inga adresser)."""
    text = str(value or "").strip()
    match = re.fullmatch(r"<[^<>\s]{3,250}>", text)
    return match.group(0) if match else ""


def _received_at(value, now):
    parsed = parse_datetime(str(value or "")) if value else None
    if parsed is None or parsed > now + timedelta(minutes=5):
        return now
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, UTC)
    return parsed


class HeaderList:
    """Notisens huvuden (mail.headers: [{"name", "value"}]) med samma
    get()/get_content_type() som ett tolkat mejl, för is_autoreply före
    hämtningen."""

    def __init__(self, headers):
        self._rows = [
            (str(h.get("name") or "").lower(), str(h.get("value") or ""))
            for h in headers or []
            if isinstance(h, dict)
        ]

    def get(self, name, default=None):
        name = name.lower()
        for key, value in self._rows:
            if key == name:
                return value
        return default

    def get_content_type(self):
        value = self.get("content-type", "") or "text/plain"
        return value.split(";", 1)[0].strip().lower() or "text/plain"


# ---------------------------------------------------------------------------
# Autosvar och citat
# ---------------------------------------------------------------------------


def is_autoreply(message):
    """Är mejlet ett autosvar, en frånvaro eller en leveransrapport (G.3
    punkt 5)? message är ett tolkat mejl (mime.parse) eller en HeaderList."""
    if message is None:
        return False
    submitted = str(message.get("Auto-Submitted", "") or "").split(";", 1)[0].strip().lower()
    if submitted and submitted != "no":
        return True
    if message.get("X-Autoreply") is not None or message.get("X-Autorespond") is not None:
        return True
    precedence = str(message.get("Precedence", "") or "").strip().lower()
    if precedence in AUTO_PRECEDENCE:
        return True
    try:
        if message.get_content_type() == "multipart/report":
            return True
    except Exception:  # noqa: BLE001 - ett trasigt huvud gör inte mejlet till ett autosvar
        pass
    subject = _one_line(message.get("Subject", ""), 200).lower()
    return subject.startswith(AUTO_SUBJECTS)


_QUOTED = re.compile(r"^\s*>")
#: Rubriken före ett citat: "Den tis 8 okt. 2026 kl 09:00 skrev Exempelrör <...>:",
#: "On Tue, Oct 8, 2026 at 9:00 AM Exempelrör <...> wrote:" och Apple Mails
#: "8 okt. 2026 kl. 09:14 skrev Exempelrör <...>:".
_ATTRIBUTION_START = re.compile(r"^\s*(?:(?:Den|On)\s|\d)", re.IGNORECASE)
_ATTRIBUTION_WORD = re.compile(r"\b(?:skrev|wrote)\b", re.IGNORECASE)
_ENDS_COLON = re.compile(r":\s*$")
#: En äkta rubrik har en adress eller ett klockslag (inte "Den 5 maj skrev jag:").
_ATTRIBUTION_PROOF = re.compile(r"@|\b\d{1,2}[:.]\d{2}\b")
_ORIGINAL = re.compile(
    r"^\s*-{2,}\s*(?:Original Message|Ursprungligt meddelande|Originalmeddelande)\s*-{2,}\s*$",
    re.IGNORECASE,
)
#: Utan intill varandra liggande \s* och \** (de backade kvadratiskt på en
#: lång rad blanksteg), och bara på radens början (LINE_CHARS).
_HEADER_FROM = re.compile(r"^[\s*]*(?:Från|From)[\s*]*:[\s*]*\S", re.IGNORECASE)
_HEADER_NEXT = re.compile(
    r"^[\s*]*(?:Skickat|Sent|Datum|Date|Till|To|Ämne|Subject)[\s*]*:", re.IGNORECASE
)
_UNDERLINE = re.compile(r"\s*_{8,}\s*")
#: Raderna prövas bara på så här många tecken (rubriker och citattecken står
#: först på raden).
LINE_CHARS = 400
#: Texten och HTML:en kortas till så här många tecken innan de tolkas. Svaret i
#: tråden är ändå högst ThreadMessage.MAX_BODY, och ett mejl på 10 MB ska inte
#: kunna hålla tick:en kvar.
TEXT_CHARS = 200_000


def _is_attribution(lines, i):
    """Rubriken före ett citat på rad i, på en rad eller (Gmail bryter långa
    rader) på två. Svarar med antalet rader den tar, annars 0. Den börjar med
    Den, On eller en siffra, har skrev eller wrote, slutar med kolon och har
    en adress eller ett klockslag, eller så följs den av citerade rader."""
    line = lines[i]
    if len(line) > 400 or not _ATTRIBUTION_START.match(line):
        return 0
    for size in (1, 2):
        if i + size > len(lines):
            break
        text = " ".join(lines[i : i + size])
        if size == 2 and len(lines[i + 1]) > 400:
            break
        if not (_ATTRIBUTION_WORD.search(text) and _ENDS_COLON.search(text)):
            continue
        after = next((n for n in lines[i + size :] if n.strip()), "")
        if _ATTRIBUTION_PROOF.search(text) or _QUOTED.match(after):
            return size
    return 0


def _cut(lines):
    """(raden där citatet börjar, slag) eller (None, "")."""
    for i, line in enumerate(lines):
        head = line[:LINE_CHARS]
        if _QUOTED.match(head):
            return i, "quote"
        if _is_attribution(lines, i):
            return i, "quote"
        if _ORIGINAL.match(head):
            return i, "header"
        if _HEADER_FROM.match(head) and any(
            _HEADER_NEXT.match(n[:LINE_CHARS]) for n in lines[i + 1 : i + 5]
        ):
            # Ett streck rakt ovanför (Outlook på webben) hör till rubriken.
            start = i
            if i > 0 and _UNDERLINE.fullmatch(lines[i - 1][:LINE_CHARS]):
                start = i - 1
            return start, "header"
    return None, ""


def _squeeze(text):
    lines = [line.rstrip() for line in text.split("\n")]
    out = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


def strip_quotes(text):
    """Svaret utan den citerade historiken (G.3 punkt 6): allt från första
    raden som börjar med >, en rubrik som "Den ... skrev:" eller "On ...
    wrote:" (också på två rader), "-----Original Message-----" eller ett
    "Från:"-block (Outlook) tas bort. Står svaret under citatet (inget över)
    behålls det som inte är citerat. Högst ThreadMessage.MAX_BODY tecken (och
    bara de första TEXT_CHARS tecknen läses)."""
    text = str(text or "")[:TEXT_CHARS]
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start, kind = _cut(lines)
    if start is None:
        return _squeeze("\n".join(lines))[: ThreadMessage.MAX_BODY]
    kept = _squeeze("\n".join(lines[:start]))
    if not kept and kind == "quote":
        rest = []
        i = start
        while i < len(lines):
            taken = _is_attribution(lines, i)
            if taken:
                i += taken
                continue
            if not _QUOTED.match(lines[i][:LINE_CHARS]):
                rest.append(lines[i])
            i += 1
        kept = _squeeze("\n".join(rest))
    return kept[: ThreadMessage.MAX_BODY]


# ---------------------------------------------------------------------------
# Innehållet
# ---------------------------------------------------------------------------

# HTML:en kommer utifrån, så inget uttryck här får backa: <head> och citatet
# letas upp med fasta strängar och korta, avgränsade kontroller, och
# radbrytningarnas uttryck har bara begränsade upprepningar.
_HEAD_OPEN = re.compile(r"<head\b", re.IGNORECASE)
_HEAD_CLOSE = re.compile(r"</head\s{0,20}>", re.IGNORECASE)
_BLOCKQUOTE = re.compile(r"<blockquote\b", re.IGNORECASE)
#: Klasserna och id:na där den citerade historiken börjar i ett HTML-mejl
#: (Gmail, Outlook, Thunderbird, Yahoo, Apple).
_QUOTE_MARKS = re.compile(
    r"gmail_quote|divRplyFwdMsg|moz-cite-prefix|yahoo_quoted|AppleOriginalContents",
    re.IGNORECASE,
)
#: Så långt bakåt från en markör letas taggens början.
_TAG_CHARS = 1000
_HTML_BREAK = re.compile(
    r"(?i)<\s{0,20}(?:br\s{0,20}/?|/p|/div|/li|/tr|/h[1-6]|/table|hr\s{0,20}/?)\s{0,20}>"
    r"|<\s{0,20}li\b[^<>]{0,2000}>"
)


def _strip_head(markup):
    """Alla <head>...</head> bort."""
    parts, pos = [], 0
    while True:
        opened = _HEAD_OPEN.search(markup, pos)
        if opened is None:
            break
        closed = _HEAD_CLOSE.search(markup, opened.end())
        if closed is None:
            break
        parts.append(markup[pos : opened.start()])
        pos = closed.end()
    parts.append(markup[pos:])
    return "".join(parts)


def _in_div_class(markup, at):
    """Var står markören på plats at? Början på <div-taggen om markören står
    i dess class- eller id-värde, annars -1."""
    start = markup.rfind("<", max(0, at - _TAG_CHARS), at)
    if start < 0 or markup[start : start + 4].lower() != "<div":
        return -1
    tag = markup[start:at].lower()
    if ">" in tag or (len(tag) > 4 and (tag[4].isalnum() or tag[4] == "_")):
        return -1
    quote = max(tag.rfind('"'), tag.rfind("'"))
    if quote < 0:
        return -1
    before = tag[:quote].rstrip()
    if not before.endswith("="):
        return -1
    before = before[:-1].rstrip()
    for name in ("class", "id"):
        if before.endswith(name):
            ahead = before[: -len(name)]
            if ahead and not (ahead[-1].isalnum() or ahead[-1] == "_"):
                return start
    return -1


def _quote_start(markup):
    """Där den citerade historiken börjar (<blockquote> eller en <div> med
    någon av markörerna i class eller id), annars -1."""
    found = _BLOCKQUOTE.search(markup)
    best = found.start() if found is not None else -1
    for mark in _QUOTE_MARKS.finditer(markup, 0, best if best >= 0 else len(markup)):
        start = _in_div_class(markup, mark.start())
        if start >= 0:
            return start if best < 0 else min(start, best)
    return best


def html_text(markup):
    """Text ur ett HTML-mejl: historiken bort, radbrytningar där blocken
    slutar, alla taggar bort med nh3 (skript och stilar med innehåll), och
    entiteterna tillbaka till tecken. Bara de första TEXT_CHARS tecknen."""
    import nh3

    markup = _strip_head(str(markup or "")[:TEXT_CHARS])
    quote = _quote_start(markup)
    if quote >= 0:
        markup = markup[:quote]
    markup = _HTML_BREAK.sub("\n", markup)
    cleaned = nh3.clean(markup, tags=set(), clean_content_tags={"script", "style", "title"})
    text = html_lib.unescape(cleaned).replace("\xa0", " ")
    return _squeeze(text)


def _decoded(part):
    """En textdels innehåll som text; okänd teckenkodning läses som UTF-8."""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError, ValueError, AssertionError, KeyError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    return str(content or "").replace("\x00", "")


def _filename(part):
    try:
        name = part.get_filename() or ""
    except Exception:  # noqa: BLE001 - ett trasigt filnamn ger en bilaga utan namn
        name = ""
    name = "".join(ch for ch in str(name) if ch.isprintable()).strip()
    return name[:120] or "bilaga"


def attachments_of(message, body=None):
    """Bilagorna som [{"name", "size"}] (filerna sparas aldrig): delar med
    Content-Disposition attachment, och delar med filnamn som inte är text
    (inbäddade bilder). Högst MAX_ATTACHMENTS."""
    found = []
    for part in message.walk():
        if part is body or part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        has_name = bool(part.get_filename()) if disposition != "attachment" else True
        if disposition != "attachment" and not (has_name and part.get_content_maintype() != "text"):
            continue
        payload = part.get_payload(decode=True)
        size = len(payload) if isinstance(payload, bytes) else 0
        found.append({"name": _filename(part), "size": size})
        if len(found) >= MAX_ATTACHMENTS:
            break
    return found


@dataclass
class Content:
    body: str = ""
    subject: str = ""
    from_address: str = ""
    message_id: str = ""
    attachments: list = field(default_factory=list)
    autoreply: bool = False


def content_of(message):
    """Det Inkorgen behöver ur ett tolkat mejl: texten utan citat,
    ämnet, From, Message-ID, bilagorna och om det är ett autosvar."""
    body_part = None
    try:
        body_part = message.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 - ett trasigt mejl visas utan text
        body_part = None
    text = ""
    if body_part is not None:
        raw = _decoded(body_part)[:TEXT_CHARS]
        text = html_text(raw) if body_part.get_content_subtype() == "html" else raw
    try:
        attachments = attachments_of(message, body_part)
    except Exception:  # noqa: BLE001 - bilagorna är bara en lista
        attachments = []
    return Content(
        body=strip_quotes(text),
        subject=_one_line(message.get("Subject", ""), 200),
        from_address=address_in(message.get("From", "")),
        message_id=_message_id(message.get("Message-ID", "")),
        attachments=attachments,
        autoreply=is_autoreply(message),
    )


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def _s3():
    from .. import aws

    return aws.client("s3")


def _error_code(exc):
    response = getattr(exc, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode", 0)
    return code, status


class _Missing(Exception):
    """Objektet finns inte längre (livscykeln, eller redan borttaget)."""


@dataclass
class Fetched:
    raw: bytes = b""
    size: int = 0
    too_big: bool = False


def _fetch(client, key, limit=MAX_BYTES, head=False):
    """Objektet ur den fasta hinken, högst limit byte. head: bara början
    (Range), för huvudena."""
    from botocore.exceptions import ClientError

    params = {"Bucket": bucket(), "Key": key}
    if head:
        params["Range"] = f"bytes=0-{limit - 1}"
    try:
        answer = client.get_object(**params)
    except ClientError as exc:
        code, status = _error_code(exc)
        if code in ("NoSuchKey", "NotFound") or status == 404:
            raise _Missing from None
        raise
    stream = answer.get("Body")
    size = int(answer.get("ContentLength") or 0)
    try:
        if not head and size > limit:
            return Fetched(size=size, too_big=True)
        raw = stream.read(limit + 1) if stream is not None else b""
    finally:
        if stream is not None and hasattr(stream, "close"):
            stream.close()
    if not head and len(raw) > limit:
        return Fetched(size=max(size, len(raw)), too_big=True)
    return Fetched(raw=raw[:limit], size=size or len(raw))


def _delete_object(key, client=None):
    """Ta bort objektet (G.3 punkt 8). Ett fel loggas bara: livscykeln (7
    dagar) och svepet tar det annars."""
    if not KEY_RE.fullmatch(str(key or "")) or not bucket():
        return False
    try:
        (client or _s3()).delete_object(Bucket=bucket(), Key=key)
    except Exception as exc:  # noqa: BLE001 - objektet tas annars av livscykeln
        logger.warning(
            "Utskick: inkommande mejl kunde inte tas bort ur hinken (%s)", type(exc).__name__
        )
        return False
    return True


def _delete_on_commit(key):
    transaction.on_commit(lambda: _delete_object(key))


# ---------------------------------------------------------------------------
# In i kön (queues.poll, i dess transaktion)
# ---------------------------------------------------------------------------


def receive(notification, now=None):
    """En notis från kön adx-utskick-inbound (G.3 punkt 1 till 4). Körs i
    queues.poll:s transaktion efter EventReceipt; inga anrop utåt (objektet
    tas bort efter commit). None när notisen inte gäller oss."""
    now = now or timezone.now()
    data = _as_dict(notification)
    if str(data.get("notificationType") or data.get("eventType") or "") != "Received":
        logger.info("Utskick: en notis som inte är ett mottaget mejl släpptes ur kön")
        return None
    key = _pinned_key(data)
    if key is None:
        logger.warning("Utskick: ett inkommande mejl från en annan hink eller nyckel släpptes")
        return None
    mail = data.get("mail") or {}
    receipt = data.get("receipt") or {}
    provider_id = str(mail.get("messageId") or "")
    if not ID_RE.fullmatch(provider_id):
        logger.warning("Utskick: ett inkommande mejl utan giltigt id släpptes")
        return None
    existing = InboundMessage.objects.filter(channel=CHANNEL_EMAIL, provider_id=provider_id).first()
    if existing is not None:
        return existing
    ref, to_address = token_from(receipt.get("recipients"))
    common = mail.get("commonHeaders") or {}
    meta = {"s3_key": key, "verdicts": _verdicts(receipt)}
    message_id = _message_id(common.get("messageId"))
    if message_id:
        meta["message_id"] = message_id
    try:
        with transaction.atomic():
            inbound = InboundMessage.objects.create(
                channel=CHANNEL_EMAIL,
                provider_id=provider_id,
                from_address=_from_of(mail),
                to_address=to_address,
                subject=_one_line(common.get("subject"), 200),
                received_at=_received_at(mail.get("timestamp"), now),
                status=InboundMessage.Status.PENDING,
                meta=meta,
                created_at=now,
            )
    except IntegrityError:
        return InboundMessage.objects.get(channel=CHANNEL_EMAIL, provider_id=provider_id)
    _triage(inbound, ref, data, now)
    logger.info("Utskick: inkommande mejl %s (%s)", inbound.pk, inbound.status)
    return inbound


def _close(inbound, status, reason="", keep_from=False):
    """Klart utan att läsa mejlet: status, orsaken, inga texter (och ingen
    adress utom när den behövs), objektet bort efter commit."""
    inbound.status = status
    inbound.body = ""
    inbound.subject = ""
    if not keep_from:
        inbound.from_address = ""
    meta = dict(inbound.meta or {})
    if reason:
        meta["reason"] = reason
    meta.pop("message_id", None)
    inbound.meta = meta
    inbound.save(
        update_fields=[
            "status",
            "body",
            "subject",
            "from_address",
            "account",
            "contact",
            "routed_via",
            "meta",
        ]
    )
    _delete_on_commit(meta.get("s3_key", ""))
    return inbound


def _ref_key(ref):
    return f"{ref.kind}{ref.account_id}.{ref.object_id}"


def _triage(inbound, ref, data, now):
    from apps.flamingo.models import FlamingoAccount

    mail = data.get("mail") or {}
    if ref is None:
        return _close(inbound, InboundMessage.Status.IGNORED, "token")
    account = FlamingoAccount.objects.filter(pk=ref.account_id).first()
    if account is None:
        return _close(inbound, InboundMessage.Status.IGNORED, "account")
    inbound.account = account
    inbound.routed_via = f"token:{ref.kind}:{ref.object_id}"[:30]
    inbound.meta = {**(inbound.meta or {}), "ref": [ref.kind, ref.object_id]}
    if ref.kind == tokens.MAILTO:
        return _mailto(inbound, ref, account, now)
    verdicts = (inbound.meta or {}).get("verdicts") or {}
    if verdicts.get("virus") == "FAIL":
        return _close(inbound, InboundMessage.Status.SPAM, "virus")
    if verdicts.get("spam") == "FAIL":
        return _close(inbound, InboundMessage.Status.SPAM, "spam")
    window = limits.hour_window(now)
    if limits.hit("inbound_mail_ref", _ref_key(ref), window, PER_REF_HOUR):
        transaction.on_commit(lambda: _alert_cap(now, per_ref=True))
        return _close(inbound, InboundMessage.Status.COUNTED, "ref_hour")
    if limits.hit("inbound_mail", "", window, PER_HOUR):
        transaction.on_commit(lambda: _alert_cap(now, per_ref=False))
        return _close(inbound, InboundMessage.Status.COUNTED, "hour")
    if is_autoreply(HeaderList(mail.get("headers"))):
        inbound.meta["auto"] = True
    inbound.save(update_fields=["account", "routed_via", "meta"])
    return inbound


def _mailto(inbound, ref, account, now):
    """Token u: ett mejl till mailto-adressen i List-Unsubscribe avregistrerar
    (G.3 punkt 7, källa list_unsub), utan att mejlet läses: adressen är
    mottagarens, eller From när mottagarraden är borta (eller tömd efter en
    GDPR-borttagning, eller ett test- eller provmejl). From gäller bara när
    avsändaren är bekräftad (from_verified); annars ignored, unverified_from,
    så att ingen med en gammal token kan avregistrera någon annans adress."""
    from .. import keys, link_actions
    from ..models import Consent, Recipient, Suppression

    recipient = (
        Recipient.objects.select_related("utskick")
        .filter(pk=ref.object_id, utskick__account=account, channel=CHANNEL_EMAIL)
        .first()
    )
    address = recipient.address if recipient is not None and recipient.address else ""
    via = "recipient" if address else "from"
    if not address and not from_verified((inbound.meta or {}).get("verdicts")):
        return _close(inbound, InboundMessage.Status.IGNORED, "unverified_from")
    address = address or inbound.from_address
    value_hash = keys.value_hash(CHANNEL_EMAIL, address) if address else ""
    if not value_hash:
        return _close(inbound, InboundMessage.Status.IGNORED, "address")
    _suppression, created, contact = link_actions.unsubscribe_email_hash(
        account,
        value_hash,
        reason=Suppression.Reason.LIST_UNSUB,
        source=Consent.Source.LIST_UNSUB,
        detail=link_actions.EMAIL_MAILTO_DETAIL,
        recipient=recipient,
        address=address,
        now=now,
    )
    inbound.contact = contact
    inbound.meta = {**(inbound.meta or {}), "keyword": "unsubscribe", "via": via, "new": created}
    return _close(inbound, InboundMessage.Status.STOP)


def _alert_cap(now, per_ref):
    if per_ref:
        lines = [
            f"Ett svar på ett utskick har fått fler än {PER_REF_HOUR} mejl på en timme.",
            "De räknas men hamnar inte i någon Inkorg.",
        ]
        once = "inbound_mail_ref"
    else:
        lines = [
            f"Fler än {PER_HOUR} mejl har kommit till svarsadressen den här timmen.",
            "Resten av timmen räknas de men hamnar inte i någon Inkorg.",
        ]
        once = "inbound_mail_hour"
    alerts.agency("Utskick: många inkommande mejl", lines, once=once, now=now)


# ---------------------------------------------------------------------------
# Efter commit: hämta, tolka och routa
# ---------------------------------------------------------------------------


def pending_exists():
    """Väntar ett inkommande mejl på att hämtas? (queues.poll_due)"""
    return InboundMessage.objects.filter(
        channel=CHANNEL_EMAIL, status=InboundMessage.Status.PENDING
    ).exists()


def process_pending(now, deadline):
    """Hämta och routa väntande mejl, äldst först, högst PER_TICK och inte
    efter deadline (time.monotonic()). Bara antal i svaret."""
    counts = {"fetched": 0, "routed": 0, "autoreply": 0, "skipped": 0, "failed": 0}
    if not bucket():
        return counts
    pks = list(
        InboundMessage.objects.filter(channel=CHANNEL_EMAIL, status=InboundMessage.Status.PENDING)
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:PER_TICK]
    )
    client = None
    for pk in pks:
        if time.monotonic() > deadline:
            break
        row = InboundMessage.objects.filter(pk=pk, status=InboundMessage.Status.PENDING).first()
        if row is None:
            continue
        key = (row.meta or {}).get("s3_key", "")
        if not KEY_RE.fullmatch(str(key)):
            _finish_unread(row.pk, InboundMessage.Status.IGNORED, "key")
            counts["skipped"] += 1
            continue
        try:
            client = client or _s3()
            fetched = _fetch(client, key)
        except _Missing:
            _finish_unread(row.pk, InboundMessage.Status.IGNORED, "missing")
            counts["skipped"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - AWS-fel: raden väntar till nästa tick
            counts["failed"] += 1
            _attempt_failed(row.pk, exc, now)
            if _is_config_error(exc):
                break
            continue
        counts["fetched"] += 1
        try:
            outcome = route(row.pk, fetched, now)
        except Exception as exc:  # noqa: BLE001 - ett mejl fäller inte fasen; försöken räknas
            logger.exception("Utskick: inkommande mejl %s kunde inte routas", row.pk)
            counts["failed"] += 1
            _attempt_failed(row.pk, exc, now)
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome != "busy":
            _delete_object(key, client)
    return counts


def _is_config_error(exc):
    from .. import aws

    return isinstance(exc, aws.AwsNotConfigured)


def _attempt_failed(pk, exc, now):
    """Hämtningen föll: försöken räknas på raden, och efter MAX_ATTEMPTS ges
    den upp (ignored) med ett larm. Objektet lämnas (livscykeln tar det)."""
    with transaction.atomic():
        row = (
            InboundMessage.objects.select_for_update()
            .filter(pk=pk, status=InboundMessage.Status.PENDING)
            .first()
        )
        if row is None:
            return
        meta = dict(row.meta or {})
        meta["attempts"] = int(meta.get("attempts") or 0) + 1
        meta["error"] = type(exc).__name__[:60]
        row.meta = meta
        fields = ["meta"]
        if meta["attempts"] >= MAX_ATTEMPTS:
            row.status = InboundMessage.Status.IGNORED
            row.subject = ""
            meta["reason"] = "fetch"
            fields += ["status", "subject"]
            transaction.on_commit(
                lambda: alerts.agency(
                    "Utskick: ett inkommande mejl kunde inte hämtas",
                    [
                        f"Inkommande {pk} kunde inte hämtas ur hinken efter {MAX_ATTEMPTS} försök "
                        f"({meta['error']}).",
                        "Kontrollera rollen adx-utskick och UTSKICK_SES_INBOUND_BUCKET.",
                    ],
                    once="inbound_mail_fetch",
                    now=now,
                )
            )
        row.save(update_fields=fields)
    logger.warning("Utskick: inkommande mejl %s kunde inte hämtas (%s)", pk, type(exc).__name__)


def _finish_unread(pk, status, reason):
    with transaction.atomic():
        row = (
            InboundMessage.objects.select_for_update()
            .filter(pk=pk, status=InboundMessage.Status.PENDING)
            .first()
        )
        if row is None:
            return
        meta = dict(row.meta or {})
        meta["reason"] = reason
        row.status = status
        row.subject = ""
        row.body = ""
        row.meta = meta
        row.save(update_fields=["status", "subject", "body", "meta"])


def _lock_address(account_id, address):
    """Ett konto och en adress i taget: två svar som routas samtidigt
    hamnar i samma tråd."""
    value = zlib.crc32(f"{account_id}:{address}".encode()) & 0x7FFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [INBOUND_MAIL_LOCK + value])


def route(pk, fetched, now=None):
    """Ett hämtat mejl till Inkorgen (G.3 punkt 5 till 7), i en transaktion.
    Svarar "routed", "autoreply", "skipped" eller "busy" (raden hanterades
    redan av någon annan)."""
    from ..email import mime

    now = now or timezone.now()
    message = None
    if not fetched.too_big and fetched.raw:
        try:
            message = mime.parse(fetched.raw)
        except Exception:  # noqa: BLE001 - ett mejl som inte går att tolka visas tomt
            message = None
    content = content_of(message) if message is not None else Content()
    with transaction.atomic():
        row = (
            InboundMessage.objects.select_for_update(of=("self",))
            .select_related("account__customer")
            .filter(pk=pk, status=InboundMessage.Status.PENDING)
            .first()
        )
        if row is None:
            return "busy"
        meta = dict(row.meta or {})
        meta["size"] = fetched.size
        if fetched.too_big:
            meta["too_big"] = True
        if content.message_id:
            meta["message_id"] = content.message_id
        row.meta = meta
        if content.from_address:
            row.from_address = content.from_address
        if content.subject:
            row.subject = content.subject
        if row.account is None or not meta.get("ref"):
            _finish(row, InboundMessage.Status.IGNORED, reason="account")
            return "skipped"
        if meta.get("auto") or content.autoreply:
            return _autoreply(row)
        if fetched.too_big:
            body, attachments = TOO_BIG_TEXT, []
        else:
            body, attachments = content.body, content.attachments
        if not body and not attachments:
            body = EMPTY_TEXT
        return _to_thread(row, body, attachments, now)


def _finish(row, status, reason=""):
    row.status = status
    row.body = ""
    row.subject = ""
    if reason:
        row.meta = {**(row.meta or {}), "reason": reason}
    row.save(
        update_fields=[
            "status",
            "body",
            "subject",
            "from_address",
            "account",
            "contact",
            "routed_via",
            "meta",
        ]
    )
    return row


@dataclass
class Target:
    """Vart ett svar ska: tråden (om token pekar på en), kontakten,
    utskicket, mottagaren och adressen svaret borde komma från."""

    thread: object = None
    contact: object = None
    utskick: object = None
    recipient: object = None
    expected: str = ""
    address: str = ""
    unverified: bool = False


def target_for(account, kind, object_id, from_address, *, verified=True):
    """Token r: mottagaren i ett utskick (tråden för mottagarens adress);
    token t: tråden i Inkorgen. Är raden borta: kontot och From (G.3 punkt 7,
    E.2), och kontakten som redan har adressen. Är From inte bekräftad
    (from_verified) kopplas svaret inte till någon kontakt (unverified)."""
    from ..models import Contact, Recipient, Thread

    target = Target()
    if kind == tokens.REPLY:
        recipient = (
            Recipient.objects.select_related("utskick", "contact")
            .filter(pk=object_id, utskick__account=account, channel=CHANNEL_EMAIL)
            .first()
        )
        if recipient is not None:
            target.recipient = recipient
            target.utskick = recipient.utskick
            target.contact = recipient.contact
            target.expected = recipient.address or (
                recipient.contact.email if recipient.contact is not None else ""
            )
            target.address = target.expected or from_address
            return target
    elif kind == tokens.THREAD:
        thread = (
            Thread.objects.select_related("lead", "contact", "utskick")
            .filter(pk=object_id, account=account, channel=CHANNEL_EMAIL)
            .first()
        )
        if thread is not None:
            target.thread = thread
            target.contact = thread.contact
            target.utskick = thread.utskick
            target.expected = thread.address
            target.address = thread.address or from_address
            return target
    target.address = from_address
    if from_address and not verified:
        target.unverified = True
    elif from_address:
        target.contact = Contact.objects.filter(account=account, email=from_address).first()
    return target


def _verified(row):
    return from_verified((row.meta or {}).get("verdicts"))


def _autoreply(row):
    """Autosvar hamnar inte i Inkorgen (G.3 punkt 5): bara på det inkommande,
    med mottagaren token gäller."""
    kind, object_id = (row.meta or {}).get("ref") or ["", 0]
    target = target_for(row.account, kind, object_id, row.from_address, verified=_verified(row))
    row.contact = target.contact
    if target.recipient is not None:
        row.meta = {**(row.meta or {}), "recipient": target.recipient.pk}
    _finish(row, InboundMessage.Status.AUTOREPLY)
    return "autoreply"


def _flagged(thread, body):
    """ "Ser ut som en avregistrering" (G.1 punkt 8) på svar på reklam: STOPP
    först, eller en av fraserna i början av svaret."""
    from . import stop

    if not stop.reklam_thread(thread):
        return False
    start = (body or "")[:500]
    return stop.classify(start) == stop.STOP or stop.looks_like_unsubscribe(start)


def _to_thread(row, body, attachments, now):
    from .. import contacts, threads
    from ..models import Recipient, ThreadMessage

    account = row.account
    kind, object_id = (row.meta or {}).get("ref") or ["", 0]
    target = target_for(account, kind, object_id, row.from_address, verified=_verified(row))
    if not target.address:
        _finish(row, InboundMessage.Status.IGNORED, reason="address")
        return "skipped"
    _lock_address(account.pk, target.address)
    thread = target.thread or threads.email_thread_for(
        account,
        target.address,
        contact=target.contact,
        utskick=target.utskick,
        now=now,
        contactless=target.unverified,
    )
    other = bool(target.expected and row.from_address and row.from_address != target.expected)
    row.body = body
    message = threads.add_inbound(
        thread, row, raise_lead=True, looks_like_stop=_flagged(thread, body), now=now
    )
    ThreadMessage.objects.filter(pk=message.pk).update(
        subject=(row.subject or "")[:200], attachments=attachments
    )
    if target.recipient is not None:
        Recipient.objects.filter(pk=target.recipient.pk, replied_at__isnull=True).update(
            replied_at=now
        )
    if thread.contact_id:
        # "Svarade på mejl" under Senast (app_views/contacts.LAST_LABELS).
        contacts.touch(thread.contact, "email_reply", now)
    meta = dict(row.meta or {})
    meta["thread"] = thread.pk
    if other:
        meta["other_address"] = True
    if target.unverified:
        meta["unverified_from"] = True
    if attachments:
        meta["attachments"] = len(attachments)
    row.meta = meta
    row.contact = thread.contact
    _finish(row, InboundMessage.Status.ROUTED)
    logger.info("Utskick: inkommande mejl %s i tråd %s", row.pk, thread.pk)
    return "routed"


# ---------------------------------------------------------------------------
# Svepet (utskick_daily)
# ---------------------------------------------------------------------------

_RECEIVED_FOR = re.compile(r"\bfor\s+<?([^\s<>;]+@[^\s<>;]+?)>?\s*;", re.IGNORECASE)


def _header_addresses(message):
    """Adresserna mejlet kan ha skickats till, ur huvudena: SES Received
    "for <...>" först, sedan X-Original-To, Delivered-To, To och Cc. Bara för
    svepet, när notisen med receipt.recipients har gått förlorad."""
    found = []
    for value in (message.get_all("Received") or [])[:20]:
        found += [m.group(1) for m in _RECEIVED_FOR.finditer(str(value)[:HEADER_CHARS])]
    for name in ("X-Original-To", "Delivered-To", "To", "Cc"):
        for value in (message.get_all(name) or [])[:5]:
            text = str(value)[:HEADER_CHARS]
            found += [m.group(1) for m in _ANGLE_RE.finditer(text)]
            found += [m.group(1) for m in _BARE_RE.finditer(text)]
    return found


def _notification_from(message, key, provider_id):
    """En notis som SES:s, byggd ur objektets huvuden (svepet)."""
    headers = []
    for name, value in message.items():
        headers.append({"name": str(name), "value": str(value)})
    return {
        "notificationType": "Received",
        "mail": {
            "messageId": provider_id,
            "timestamp": "",
            "source": address_in(message.get("Return-Path", "")),
            "headers": headers,
            "commonHeaders": {
                "from": [str(message.get("From", "") or "")],
                "subject": str(message.get("Subject", "") or ""),
                "messageId": str(message.get("Message-ID", "") or ""),
            },
        },
        "receipt": {
            "recipients": _header_addresses(message),
            "spamVerdict": {"status": str(message.get("X-SES-Spam-Verdict", "") or "")},
            "virusVerdict": {"status": str(message.get("X-SES-Virus-Verdict", "") or "")},
            "action": {"type": "S3", "bucketName": bucket(), "objectKey": key},
        },
    }


def sweep_bucket(now=None):
    """utskick_daily (G.3 punkt 8): in/-objekt äldre än SWEEP_AGE. Ett objekt
    utan rad (notisen gick förlorad) läses från huvudet och tas emot som en
    notis; ett objekt vars rad är klar tas bort; ett väntande lämnas till
    process_pending, som körs sist. Bara antal i svaret."""
    from ..email import mime
    from ..models import EventReceipt

    now = now or timezone.now()
    counts = {"listed": 0, "orphans": 0, "deleted": 0, "failed": 0}
    if not queues.inbound_enabled():
        return counts
    try:
        client = _s3()
    except Exception as exc:  # noqa: BLE001 - utan AWS finns inget att svepa
        logger.warning("Utskick: svepet av hinken fick ingen AWS-klient (%s)", type(exc).__name__)
        counts["failed"] += 1
        return counts
    cutoff = now - SWEEP_AGE
    keys = []
    params = {"Bucket": bucket(), "Prefix": PREFIX, "MaxKeys": 1000}
    for _ in range(SWEEP_PAGES):
        try:
            page = client.list_objects_v2(**params)
        except Exception as exc:  # noqa: BLE001 - nästa dygn försöker igen
            logger.warning("Utskick: hinken kunde inte listas (%s)", type(exc).__name__)
            counts["failed"] += 1
            break
        for item in page.get("Contents") or []:
            key = str(item.get("Key") or "")
            modified = item.get("LastModified")
            if not KEY_RE.fullmatch(key) or (modified is not None and modified > cutoff):
                continue
            keys.append(key)
        if not page.get("IsTruncated") or not page.get("NextContinuationToken"):
            break
        params["ContinuationToken"] = page["NextContinuationToken"]
    counts["listed"] = len(keys)
    by_id = {key[len(PREFIX) :]: key for key in keys}
    known = dict(
        InboundMessage.objects.filter(
            channel=CHANNEL_EMAIL, provider_id__in=list(by_id)
        ).values_list("provider_id", "status")
    )
    for provider_id, key in by_id.items():
        status = known.get(provider_id)
        if status == InboundMessage.Status.PENDING:
            continue
        if status is not None:
            if _delete_object(key, client):
                counts["deleted"] += 1
            continue
        if counts["orphans"] >= SWEEP_MAX or not ID_RE.fullmatch(provider_id):
            continue
        try:
            head = _fetch(client, key, limit=HEAD_BYTES, head=True)
            message = mime.parse(head.raw)
            notification = _notification_from(message, key, provider_id)
            with transaction.atomic():
                EventReceipt.objects.get_or_create(key=f"in:{provider_id}"[:140])
                receive(notification, now)
        except _Missing:
            continue
        except Exception as exc:  # noqa: BLE001 - ett objekt fäller inte svepet
            logger.warning("Utskick: svepet kunde inte ta emot ett objekt (%s)", type(exc).__name__)
            counts["failed"] += 1
            continue
        counts["orphans"] += 1
    if counts["orphans"]:
        logger.warning("Utskick: svepet hittade %s inkommande mejl utan notis", counts["orphans"])
    process_pending(now, time.monotonic() + SWEEP_SECONDS)
    return counts
