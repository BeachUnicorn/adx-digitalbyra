"""
Brevs block: validering, nya fältsorter och sparningen (README F.1, F.2,
F.3, H.1).

Utskick.email_doc = {"blocks": [...]} med sidbyggarens block-JSON (id,
type, variant, active, versions, sig; apps/flamingo/pagebuilder/__init__.py).
Versionernas signatur är pagebuilder.blocks.sign_version med ett eget salt
(SIGN_SALT), så att en version som kopierats från en sida aldrig behåller en
giltig "ai"-källa.

    SIGN_SALT = "apps.utskick.email.version"
    class StaleRevision(Exception)      409 i brev_save (som pagebuilder/pages.save_draft);
                                        .current är rev som gäller
    class BlockError(ValueError)        .errors: [{"block", "field", "where", "text"}]
    class BlockUnavailable(BlockError)  blocket är låst (registry.available)
    new_block(type_key, account, utskick, *, user=None, now=None) -> dict
                                        mallens innehåll ur Företagets uppgifter
    add_version(block, fields, source, user, *, account, utskick=None,
                activate=True, now=None) -> version
                                        AI:s eller redigerarens nya version (rensad)
    validate(account, utskick, blocks) -> list[dict]
                                        rensade block; BlockError med alla fel,
                                        access.ForeignIds (vyn svarar 400) för en
                                        bild som inte är kontots i en aktiv version
    save(utskick, blocks, *, rev, user=None, account=None, now=None,
         terms_ok=False) -> int         vem som skrev vad (som sidornas
                                        stamp_authorship), validera, signera, villkorlig
                                        filter(pk, email_rev=rev, status i EDITABLE)
                                        .update(email_doc, email_rev + 1, confirmed_terms);
                                        StaleRevision vid fel rev. Villkoren ur
                                        erbjudandeblocken (terms_from) går till
                                        confirmed_terms; ändras de nollas
                                        terms_confirmed_at. terms_ok med en användare:
                                        kunden bockade "Uppgifterna stämmer" (F.7).
                                        Nya länkvärdar blir väntande (links.request_if_new).
    clean_url(account, value, *, allow_pending=True) -> str
    own_page(account, url) -> Campaign | None   kontots egen Flamingo-sida (S3, integrationen)
                                        F.1 url: absolut http, https, mailto eller tel,
                                        högst 500, klick-id bort, E.8 för http(s)
                                        (links.clean_external), aldrig sammanfogning;
                                        links.LinkRefused (eller HostPending) med texten
    parse_rich(text, *, bold_only=False) -> list
                                        rich_basic som AST, aldrig HTML (formen nedan)
    merge_problems(account, text, *, field_keys=None) -> list[str]
                                        platshållarna i en text (F.3): okända, sms-only
                                        och fält som inte finns (ämnesrad och förhandstext
                                        prövas också med den)
    media_ids(blocks) -> set[int]       alla media-id i blocken, alla versioner
    urls(blocks) -> list[tuple]         (block_id, plats, adress, etikett) för de spårade
                                        länkarna, i renderarens ordning (båda tar
                                        blocklistan eller det active_blocks ger)
    terms_from(blocks) -> list[dict]    [{"label", "value"}] ur erbjudandeblocken
    active_blocks(utskick, doc=None) -> list[(block, BlockType, fields)]
                                        blocken med en känd typ och den aktiva
                                        versionens fält (render och text)

Fältsorterna (registry.py): text, textarea, media, items och choice som
sidornas; url, date (ISO, "2026-10-24"), time ("15:00"), email, phone
(sparas som E.164, ritas "08-123 456 78"), code ([A-Z0-9-]{3,20}, stora
bokstäver) och rich_basic. Allt är vanlig text, aldrig HTML: HTML tas bort
och AI-typografi normaliseras (apps.common.security, som sidornas fält).
Obligatoriska fält och listornas minsta antal stoppar inte sparningen (ett
utkast sparas medan kunden skriver); email/checks.py blockerar dem. Ett fält
som tagits bort ur en typ (registry.RETIRED_FIELDS, underskriftens
script_name) nekas inte: det följer inte med när mejlet sparas, och en
version som bara skiljer sig i det behåller källa, by och at.

rich_basic: tom rad = nytt stycke, **fet**, *kursiv*, [text](adress) och
rader som börjar med "- " (en punktlista). bold_only tar bara **fet**; resten
står kvar som text. AST:

    [{"t": "p", "c": [inline, ...]},
     {"t": "ul", "items": [[inline, ...], ...]}]
    inline: {"t": "text", "v": "..."} | {"t": "br"} | {"t": "b", "c": [...]}
            | {"t": "i", "c": [...]} | {"t": "a", "href": "https://...", "c": [...]}

Sammanfogningen (F.3) är composer.merge (S2) och görs i renderaren på
textbitarna efter tolkningen, så att ett värde aldrig blir en länk eller
fetstil. Inga platshållare i url-fält eller länkadresser.
"""

import copy
import re
from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import F
from django.utils import timezone

from apps.flamingo.pagebuilder import blocks as pb
from apps.flamingo.pagebuilder.registry import CHOICE, ITEMS, MEDIA, TEXT, TEXTAREA

from . import registry
from .registry import (
    CODE,
    DATE,
    EMAIL,
    EMAIL_TYPES,
    MAX_BLOCKS,
    PHONE,
    RICH_BASIC,
    TIME,
    URL,
    VARIANT,
)

SIGN_SALT = "apps.utskick.email.version"

SOURCE_TEMPLATE = pb.SOURCE_TEMPLATE
SOURCE_AI = pb.SOURCE_AI
SOURCE_CUSTOMER = pb.SOURCE_CUSTOMER
SOURCE_ADX = pb.SOURCE_ADX

URL_MAX = 500
EMAIL_MAX = 254

_TIME_RE = re.compile(r"([01]?\d|2[0-3])[:.]([0-5]\d)")  # fullmatch
_CODE_RE = re.compile(r"[A-Z0-9-]{3,20}")  # fullmatch
_TAG_IN_URL = re.compile(r"[{}]")
#: Klick-id som tas bort ur en adress utöver links.STRIP_PARAMS (F.1).
EXTRA_CLICK_IDS = frozenset({"fbclid", "msclkid", "dclid", "yclid", "twclid", "ttclid"})

LOCKED_TEXT = "Utskicket går inte att ändra nu."
URL_TAG_TEXT = "Platshållare går inte i en adress."
URL_SCHEME_TEXT = "Skriv hela adressen, med https:// först, eller mailto: eller tel:."
MAILTO_TEXT = "Skriv en hel e-postadress efter mailto:."
TEL_TEXT = "Skriv ett telefonnummer efter tel:."
DATE_TEXT = "Skriv datumet som åååå-mm-dd."
TIME_TEXT = "Skriv tiden som 15:00."
EMAIL_TEXT = "Skriv en hel e-postadress, till exempel anna@exempelror.example."
PHONE_TEXT = "Skriv ett telefonnummer, till exempel 08-123 456 78."
CODE_TEXT = "Koden får ha 3 till 20 tecken: A till Z, siffror och bindestreck."
MEDIA_TEXT = "Bilden finns inte i ditt mediaarkiv."
TEXT_TYPE_TEXT = "Ska vara text."


class StaleRevision(Exception):
    """Någon annan sparade emellan (fel email_rev). current: rev som gäller."""

    def __init__(self, current=None):
        super().__init__("Mejlet har ändrats sedan du öppnade det.")
        self.current = current


class BlockError(ValueError):
    """Blocken gick inte igenom valideringen. errors: [{"block", "field",
    "where", "text"}] (block är blockets id, field fältets sökväg som
    "items.0.url", where var det sitter i klartext)."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [_error("", "", "", errors)]
        super().__init__("Blocken är inte giltiga.")
        self.errors = list(errors)

    @property
    def texts(self):
        return [f"{e['where']}: {e['text']}" if e.get("where") else e["text"] for e in self.errors]


class BlockUnavailable(BlockError):
    """Blocket går inte att lägga till (registry.available)."""


def _error(block_id, path, where, text):
    return {"block": block_id or "", "field": path or "", "where": where or "", "text": text}


# ---------------------------------------------------------------------------
# rich_basic
# ---------------------------------------------------------------------------

#: [text](adress): texten på en rad utan klamrar, adressen utan blanksteg.
RICH_LINK = re.compile(r"\[([^\[\]\n]{1,300})\]\(([^()\s]{1,600})\)")
_BOLD = r"\*\*(?P<b>[^\s*](?:.*?[^\s*])?)\*\*"
_ITALIC = r"(?<![*\w])\*(?P<i>[^\s*](?:[^*\n]*?[^\s*])?)\*(?![*\w])"
_LINK = r"\[(?P<lt>[^\[\]\n]{1,300})\]\((?P<href>[^()\s]{1,600})\)"
_INLINE_FULL = re.compile(f"{_BOLD}|{_LINK}|{_ITALIC}")
_INLINE_BOLD = re.compile(_BOLD)
_INLINE_NO_LINK = re.compile(f"{_BOLD}|{_ITALIC}")
_PARAGRAPHS = re.compile(r"\n[ \t]*\n")
LIST_PREFIX = "- "


def _text_node(value):
    return {"t": "text", "v": value}


def _inline(text, pattern, depth=0):
    """Textbitar, fetstil, kursiv och länkar ur en rad (utan radbrytningar)."""
    out = []
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            out.append(_text_node(text[pos : match.start()]))
        if match.group("b") is not None:
            inner = _INLINE_NO_LINK if pattern is _INLINE_FULL else pattern
            children = _inline(match.group("b"), inner, depth + 1) if depth < 2 else []
            out.append({"t": "b", "c": children or [_text_node(match.group("b"))]})
        elif "href" in match.groupdict() and match.group("href") is not None:
            children = _inline(match.group("lt"), _INLINE_NO_LINK, depth + 1) if depth < 2 else []
            out.append(
                {
                    "t": "a",
                    "href": match.group("href"),
                    "c": children or [_text_node(match.group("lt"))],
                }
            )
        elif "i" in match.groupdict() and match.group("i") is not None:
            out.append({"t": "i", "c": [_text_node(match.group("i"))]})
        pos = match.end()
    if pos < len(text):
        out.append(_text_node(text[pos:]))
    return out


def _lines(lines, pattern):
    """Rader i ett stycke som inline-noder med radbrytningar emellan."""
    out = []
    for n, line in enumerate(lines):
        if n:
            out.append({"t": "br"})
        out.extend(_inline(line, pattern))
    return out


def parse_rich(text, *, bold_only=False):
    """rich_basic som AST (formen i modulens beskrivning). Tom text ger []."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    pattern = _INLINE_BOLD if bold_only else _INLINE_FULL
    out = []
    for chunk in _PARAGRAPHS.split(text):
        lines = [line.rstrip() for line in chunk.split("\n") if line.strip()]
        if not lines:
            continue
        group, kind = [], None
        for line in lines:
            is_item = not bold_only and line.lstrip().startswith(LIST_PREFIX)
            this = "ul" if is_item else "p"
            if kind is not None and this != kind:
                out.append(_node(kind, group, pattern))
                group = []
            kind = this
            group.append(line.lstrip()[len(LIST_PREFIX) :].strip() if is_item else line.strip())
        if group:
            out.append(_node(kind, group, pattern))
    return out


def _node(kind, group, pattern):
    if kind == "ul":
        return {"t": "ul", "items": [_inline(item, pattern) for item in group]}
    return {"t": "p", "c": _lines(group, pattern)}


def rich_links(ast):
    """Länkarnas adresser i AST:n, i ordning."""
    found = []

    def walk(nodes):
        for node in nodes:
            if node.get("t") == "a":
                found.append(node.get("href", ""))
            walk(node.get("c") or [])
            for item in node.get("items") or []:
                walk(item)

    walk(ast)
    return found


def rich_plain(ast):
    """AST:n som läsbar text utan markeringar (kontrollerna, reklamorden)."""
    parts = []

    def inline(nodes):
        out = []
        for node in nodes:
            kind = node.get("t")
            if kind == "text":
                out.append(node.get("v", ""))
            elif kind == "br":
                out.append("\n")
            else:
                out.append(inline(node.get("c") or []))
        return "".join(out)

    for node in ast:
        if node.get("t") == "ul":
            parts.extend("- " + inline(item) for item in node.get("items") or [])
        else:
            parts.append(inline(node.get("c") or []))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Platshållarna (F.3)
# ---------------------------------------------------------------------------


def account_field_keys(account):
    """Kontots extrafälts nycklar ({fält:<nyckel>})."""
    from ..models import FieldDef

    return set(FieldDef.objects.filter(account=account).values_list("key", flat=True))


def merge_problems(account, text, *, field_keys=None):
    """Fel i platshållarna som svensk text (F.3): det som inte är en
    platshållare, sms-platshållarna {länk:...} och {avregistrering}, och
    fält som inte finns. Tom lista när texten går att använda."""
    from .. import composer

    found = composer.placeholders(text or "")
    errors = []
    for token in found.unknown:
        errors.append(
            f"{token} är ingen platshållare. Använd {{förnamn}}, {{efternamn}}, {{namn}}, "
            "{företag} eller {fält:...}, eller ta bort klamrarna."
        )
    for key in found.links:
        errors.append(
            f"{{länk:{key}}} går bara i sms. Lägg adressen i ett fält för länkar i stället."
        )
    if found.unsubscribe:
        errors.append("{avregistrering} går bara i sms. Avregistreringen står alltid i sidfoten.")
    if found.fields:
        if field_keys is None:
            field_keys = account_field_keys(account) if account is not None else set()
        for key in found.fields:
            if key not in field_keys:
                errors.append(
                    f"Fältet {{fält:{key}}} finns inte. Skapa det under Kontakter, Fält, "
                    "eller ta bort det ur texten."
                )
    return errors


# ---------------------------------------------------------------------------
# Adresser (F.1 url, E.8)
# ---------------------------------------------------------------------------


def _strip_click_ids(url):
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in EXTRA_CLICK_IDS]
    if len(kept) == len(pairs):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _clean_mailto(text):
    from urllib.parse import urlsplit

    address = urlsplit(text).path.strip()
    try:
        validate_email(address)
    except ValidationError:
        raise _refused(MAILTO_TEXT) from None
    query = text.split("?", 1)[1] if "?" in text else ""
    cleaned = "mailto:" + address.lower()
    return f"{cleaned}?{query}" if query else cleaned


def _clean_tel(text):
    number = phone_e164(text[4:])
    if not number:
        raise _refused(TEL_TEXT)
    return "tel:" + number


def _refused(text):
    from .. import links

    return links.LinkRefused(text)


def clean_url(account, value, *, allow_pending=True):
    """En adress i ett url-fält eller en länk i rich_basic, rensad (F.1).
    "" för en tom adress. Kastar links.LinkRefused med texten för kunden
    (HostPending när värden väntar på ADX och allow_pending är av)."""
    from .. import links

    text = str(value or "").strip()
    if not text:
        return ""
    if _TAG_IN_URL.search(text):
        raise links.LinkRefused(URL_TAG_TEXT)
    if len(text) > URL_MAX:
        raise links.LinkRefused(links.TOO_LONG_TEXT)
    lowered = text.lower()
    if lowered.startswith("mailto:"):
        return _clean_mailto(text)
    if lowered.startswith("tel:"):
        return _clean_tel(text)
    if not lowered.startswith(("http://", "https://")):
        raise links.LinkRefused(URL_SCHEME_TEXT)
    # S3 (integrationen): kontots egen Flamingo-sida prövas inte mot
    # värdarna (E.8); frysningen gör den till en lp-länk med ut.
    if own_page(account, text) is not None:
        return _strip_click_ids(text)
    cleaned = links.clean_external(account, text, allow_pending=allow_pending)
    return _strip_click_ids(cleaned)


def own_page(account, url, *, pages=None):
    """Kampanjen när adressen är en av kontots egna Flamingo-sidor
    (https://adx.se/lp/<slug>/, exports.landing_page_url), annars None.
    En sådan länk är alltid kontots egen: den begärs aldrig hos ADX, prövas
    inte i Granska, och frysningen gör den till en TrackedLink av slaget lp
    (sending.email.freeze_email), så att klicket får ut och förfrågan
    spåret. En annan kunds sida på samma värd prövas som vilken adress som
    helst (S3, integrationen). Ankare, fråga och snedstreck sist räknas
    inte. pages: sending.email.own_pages(account) när den redan finns (en
    fråga i stället för en per länk). Den enda tolkningen: redigeraren,
    Granska och frysningen använder alla den här."""
    from urllib.parse import urlsplit

    from apps.flamingo.exports import landing_host

    text = str(url or "").strip()
    parts = urlsplit(text)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host != landing_host() or not (parts.path or "").startswith("/lp/"):
        return None
    if pages is None:
        from ..sending.email import own_pages

        pages = own_pages(account)
    key = text.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return pages.get(key)


def phone_e164(raw):
    """Ett telefonnummer (fast eller mobilt) som E.164, eller "". Svenska
    nummer utan landsnummer tolkas som svenska."""
    import phonenumbers

    text = str(raw or "").strip()
    if not text or len(text) > 40:
        return ""
    if text.startswith("00"):
        text = "+" + text[2:]
    try:
        parsed = phonenumbers.parse(text, "SE")
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(parsed):
        return ""
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


# ---------------------------------------------------------------------------
# Fälten
# ---------------------------------------------------------------------------


class _Cleaner:
    """Rensar en versions fält mot typens schema. errors får felen;
    urls får de rensade http(s)-adresserna (nya värdar begärs efter
    sparningen)."""

    def __init__(self, account, *, media_ids=None, field_keys=None):
        self.account = account
        self.media_ids = media_ids
        self.field_keys = field_keys
        self.errors = []
        self.urls = []

    def _err(self, block_id, path, where, text):
        self.errors.append(_error(block_id, path, where, text))

    def fields(self, block_type, fields, block_id, where):
        if not isinstance(fields, dict):
            self._err(block_id, "", where, "Fälten ska vara ett dict.")
            return {}
        # Ett borttaget fält (registry.RETIRED_FIELDS) i ett sparat mejl är
        # inget fel: det följer bara inte med.
        retired = registry.RETIRED_FIELDS.get(block_type.key, frozenset())
        unknown = set(fields) - {spec.key for spec in block_type.fields} - retired
        if unknown:
            self._err(block_id, "", where, f"Okända fält {', '.join(sorted(unknown))}.")
        return {
            spec.key: self.value(
                spec, fields.get(spec.key), block_id, spec.key, f"{where}, {spec.label}"
            )
            for spec in block_type.fields
        }

    def _tags(self, text, block_id, path, where):
        for problem in merge_problems(self.account, text, field_keys=self._keys()):
            self._err(block_id, path, where, problem)

    def _keys(self):
        if self.field_keys is None:
            self.field_keys = (
                account_field_keys(self.account) if self.account is not None else set()
            )
        return self.field_keys

    def _length(self, text, spec, block_id, path, where):
        if spec.max_length and len(text) > spec.max_length:
            self._err(block_id, path, where, f"Högst {spec.max_length} tecken (nu {len(text)}).")

    def _string(self, value, block_id, path, where):
        if value is None:
            return None
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            self._err(block_id, path, where, TEXT_TYPE_TEXT)
            return None
        return str(value)

    def value(self, spec, value, block_id, path, where):
        kind = spec.kind
        if kind in (TEXT, TEXTAREA, RICH_BASIC):
            raw = self._string(value, block_id, path, where)
            if not raw:
                return ""
            limit = spec.max_length or 1000
            text = pb._plain(raw, limit) if kind == TEXT else pb._multi(raw, limit)
            self._length(text, spec, block_id, path, where)
            self._tags(text, block_id, path, where)
            if kind == RICH_BASIC:
                text = self._rich_links(text, spec, block_id, path, where)
            return text
        if kind == URL:
            raw = self._string(value, block_id, path, where)
            if not raw:
                return ""
            return self._url(raw, block_id, path, where)
        if kind == DATE:
            raw = (self._string(value, block_id, path, where) or "").strip()
            if not raw:
                return ""
            try:
                if len(raw) != 10:
                    raise ValueError(raw)
                return date.fromisoformat(raw).isoformat()
            except ValueError:
                self._err(block_id, path, where, DATE_TEXT)
                return ""
        if kind == TIME:
            raw = (self._string(value, block_id, path, where) or "").strip()
            if not raw:
                return ""
            match = _TIME_RE.fullmatch(raw)
            if not match:
                self._err(block_id, path, where, TIME_TEXT)
                return ""
            return f"{int(match.group(1)):02d}:{match.group(2)}"
        if kind == EMAIL:
            raw = (self._string(value, block_id, path, where) or "").strip()
            if not raw:
                return ""
            address = raw.lower()
            try:
                if len(address) > EMAIL_MAX:
                    raise ValidationError("lång")
                validate_email(address)
            except ValidationError:
                self._err(block_id, path, where, EMAIL_TEXT)
                return ""
            return address
        if kind == PHONE:
            raw = (self._string(value, block_id, path, where) or "").strip()
            if not raw:
                return ""
            number = phone_e164(raw)
            if not number:
                self._err(block_id, path, where, PHONE_TEXT)
                return ""
            return number
        if kind == CODE:
            raw = (self._string(value, block_id, path, where) or "").strip()
            if not raw:
                return ""
            code = "".join(raw.split()).upper()
            if not _CODE_RE.fullmatch(code):
                self._err(block_id, path, where, CODE_TEXT)
                return ""
            return code
        if kind == CHOICE:
            values = [c[0] for c in spec.choices]
            if value in (None, ""):
                return values[0] if values else ""
            if value not in values:
                self._err(block_id, path, where, f"Välj ett av {', '.join(values)}.")
                return values[0] if values else ""
            return value
        if kind == MEDIA:
            if value in (None, ""):
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                self._err(block_id, path, where, "Ska vara en bild ur mediaarkivet.")
                return None
            if self.media_ids is not None and value not in self.media_ids:
                self._err(block_id, path, where, MEDIA_TEXT)
                return None
            return value
        if kind == ITEMS:
            return self._items(spec, value, block_id, path, where)
        self._err(block_id, path, where, f"Okänd fältsort {kind}.")
        return None

    def _url(self, raw, block_id, path, where):
        from .. import links

        try:
            cleaned = clean_url(self.account, raw, allow_pending=True)
        except links.LinkRefused as exc:
            self._err(block_id, path, where, str(exc))
            return ""
        if cleaned.startswith(("http://", "https://")) and own_page(self.account, cleaned) is None:
            self.urls.append(cleaned)
        return cleaned

    def _rich_links(self, text, spec, block_id, path, where):
        """Länkarna i en rich_basic-text rensade på plats (klick-id bort,
        värden prövade). bold_only har inga länkar."""
        if spec.bold_only:
            return text

        def replace(match):
            cleaned = self._url(match.group(2), block_id, path, where)
            return f"[{match.group(1)}]({cleaned or match.group(2)})"

        return RICH_LINK.sub(replace, text)

    def _items(self, spec, value, block_id, path, where):
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            self._err(block_id, path, where, "Ska vara en lista.")
            return []
        items = []
        for i, raw in enumerate(value):
            item_where = f"{where}, nummer {i + 1}"
            if not isinstance(raw, dict):
                self._err(block_id, f"{path}.{i}", item_where, "Ska vara en post med fält.")
                continue
            unknown = set(raw) - {sub.key for sub in spec.items}
            if unknown:
                self._err(
                    block_id,
                    f"{path}.{i}",
                    item_where,
                    f"Okända fält {', '.join(sorted(unknown))}.",
                )
            item = {
                sub.key: self.value(
                    sub,
                    raw.get(sub.key),
                    block_id,
                    f"{path}.{i}.{sub.key}",
                    f"{item_where}, {sub.label}",
                )
                for sub in spec.items
            }
            if not any(
                v not in ("", None, []) for k, v in item.items() if spec.sub(k).kind != CHOICE
            ):
                continue
            items.append(item)
        if spec.max_items and len(items) > spec.max_items:
            self._err(block_id, path, where, f"Högst {spec.max_items} (nu {len(items)}).")
        return items


def clean_fields(account, block_type, fields, *, block_id="", where="", media_ids=None):
    """Fälten rensade mot typens schema (BlockError med felen). För AI:s
    och redigerarens nya versioner (add_version)."""
    cleaner = _Cleaner(account, media_ids=media_ids)
    cleaned = cleaner.fields(block_type, fields, block_id, where or block_type.name)
    if cleaner.errors:
        raise BlockError(cleaner.errors)
    return cleaned


# ---------------------------------------------------------------------------
# Blocken
# ---------------------------------------------------------------------------


def _now_iso(now=None):
    return (now or timezone.now()).isoformat()


def _user_id(user):
    pk = getattr(user, "pk", None)
    return pk if isinstance(pk, int) and not isinstance(pk, bool) else None


def _version(fields, source, user, now=None):
    return pb.sign_version(
        {
            "id": pb.new_version_id(),
            "fields": fields,
            "source": source,
            "by": _user_id(user),
            "at": _now_iso(now),
        },
        salt=SIGN_SALT,
    )


def is_signed(version):
    """Har versionen e-postens signatur (SIGN_SALT), oförändrad?"""
    return pb.is_signed(version, salt=SIGN_SALT)


def active_version(block):
    return pb.active_version(block)


def active_fields(block):
    """Den aktiva versionens fält med tomma värden för det som saknas."""
    return pb.active_fields(block, types=EMAIL_TYPES)


def _template_fields(type_key, account, utskick, user):
    """Mallens innehåll för ett nytt block: bara det Företaget och
    inloggningen säger, aldrig något påhittat."""
    from apps.flamingo.pagebuilder.facts import facts_for

    facts = facts_for(account) if account is not None else None
    phone = phone_e164(facts.phone) if facts is not None else ""
    if type_key == "heading":
        return {"size": "h2"}
    if type_key == "button":
        return {"align": "left"}
    if type_key == "image_text":
        return {"side": "left"}
    if type_key == "columns":
        return {"items": [{"number": f"0{n}", "title": "", "text": ""} for n in (1, 2, 3)]}
    if type_key == "prices":
        rows = facts.prices[:10] if facts is not None else []
        return {
            "title": "Priser" if rows else "",
            "items": [{"name": label[:60], "price": value[:30]} for label, value in rows],
        }
    if type_key == "reviews":
        sources = registry.review_sources(account) if account is not None else {}
        source = next((key for key, ok in sources.items() if ok), "google")
        return {"source": source, "count": "2", "show_summary": "yes"}
    if type_key == "steps":
        return {
            "title": "Så går det till",
            "items": [
                {"text": "**Boka** via knappen."},
                {"text": "**Vi bekräftar** tiden med dig."},
                {"text": "**Klart.**"},
            ],
        }
    if type_key == "event":
        return {"calendar": "yes"}
    if type_key == "person":
        return {
            "name": (facts.person if facts is not None else "")[:60],
            "phone": phone,
            "email": (facts.email if facts is not None else "")[:254],
        }
    if type_key == "faq":
        return {"title": "Vanliga frågor"}
    if type_key == "hours":
        return {"show_hours": "yes", "show_address": "yes", "map_text": "Hitta hit"}
    if type_key == "signature":
        name = ""
        if user is not None and not getattr(user, "is_staff", False):
            name = " ".join(
                p for p in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if p
            )
        name = name or (facts.person if facts is not None else "")
        return {
            "greeting": "Vänliga hälsningar,",
            "name": name[:60],
            "line": (facts.company if facts is not None else "")[:120],
            "phone": phone,
        }
    if type_key == "spacer":
        return {"size": "m"}
    if type_key == "social":
        return {"items": []}
    return {}


def _lenient(account, block_type, fields):
    """Mallens fält rensade; ett värde som inte klarar schemat (en uppgift
    med ett nummer som inte går att tolka) blir tomt i stället för ett fel."""
    cleaner = _Cleaner(account)
    cleaned = cleaner.fields(block_type, fields, "", block_type.name)
    for error in cleaner.errors:
        key = error["field"].split(".")[0]
        if key in cleaned:
            cleaned[key] = block_type.field(key).empty()
    return cleaned


def new_block(type_key, account, utskick, *, user=None, now=None):
    """Ett nytt block med en version (källan mallen), signerat. Kastar
    BlockError för en okänd typ och BlockUnavailable för ett låst block."""
    block_type = EMAIL_TYPES.get(type_key)
    if block_type is None:
        raise BlockError(f"Okänd blocktyp: {type_key}")
    ok, why = registry.available(account, utskick).get(type_key, (True, ""))
    if not ok:
        raise BlockUnavailable([_error("", "", block_type.name, why)])
    fields = _lenient(account, block_type, _template_fields(type_key, account, utskick, user))
    version = _version(fields, SOURCE_TEMPLATE, user, now)
    return {
        "id": pb.new_block_id(),
        "type": type_key,
        "variant": VARIANT,
        "active": version["id"],
        "versions": [version],
    }


def add_version(block, fields, source, user, *, account, utskick=None, activate=True, now=None):
    """En ny version (rensad mot schemat, BlockError om något inte går),
    aktiv om inte activate=False. Högst pagebuilder.MAX_VERSIONS: de
    äldsta som inte är aktiva tas bort. Blocket ändras på plats."""
    block_type = EMAIL_TYPES.get(block.get("type"))
    if block_type is None:
        raise BlockError(f"Okänd blocktyp: {block.get('type')}")
    if source not in pb.SOURCES:
        raise BlockError(f"Okänd källa för versionen: {source}")
    from apps.flamingo.models import MediaAsset

    media = None
    if account is not None:
        media = set(MediaAsset.objects.filter(account=account).values_list("pk", flat=True))
    cleaned = clean_fields(
        account, block_type, fields, block_id=block.get("id", ""), media_ids=media
    )
    version = _version(cleaned, source, user, now)
    block.setdefault("versions", []).append(version)
    if activate or not block.get("active"):
        block["active"] = version["id"]
    while len(block["versions"]) > pb.MAX_VERSIONS:
        oldest = next(v for v in block["versions"] if v["id"] != block["active"])
        block["versions"].remove(oldest)
    return version


def doc_blocks(utskick, doc=None):
    """Blocklistan ur email_doc (eller doc), [] när den saknas."""
    doc = utskick.email_doc if doc is None else doc
    blocks = doc.get("blocks") if isinstance(doc, dict) else None
    return [b for b in blocks or [] if isinstance(b, dict)]


def active_blocks(utskick, doc=None):
    """[(block, BlockType, fields)] för blocken med en känd typ, i ordning,
    med den aktiva versionens fält."""
    out = []
    for block in doc_blocks(utskick, doc):
        block_type = EMAIL_TYPES.get(block.get("type"))
        if block_type is None:
            continue
        out.append((block, block_type, active_fields(block)))
    return out


def as_blocks(items):
    """Blocken som dict: tar både blocklistan ur email_doc och det
    active_blocks ger ((block, BlockType, fält))."""
    out = []
    for item in items or []:
        if isinstance(item, tuple) and item and isinstance(item[0], dict):
            out.append(item[0])
        elif isinstance(item, dict):
            out.append(item)
    return out


def media_ids(blocks):
    """Alla media-id i blocken, alla versioner (en version kan väljas igen).
    blocks: blocklistan eller active_blocks(...)."""
    from apps.flamingo.media import media_ids_in

    return media_ids_in(as_blocks(blocks), types=EMAIL_TYPES)


def _active_media_ids(blocks):
    """Bilderna i de aktiva versionerna (utan aktiv: den sista, som
    pagebuilder.active_version)."""
    ids = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("versions"), list):
            continue
        versions = [v for v in block["versions"] if isinstance(v, dict)]
        version = next((v for v in versions if v.get("id") == block.get("active")), None)
        version = version or (versions[-1] if versions else None)
        if version is None:
            continue
        for asset_id in media_ids([{**block, "versions": [version]}]):
            if asset_id not in ids:
                ids.append(asset_id)
    return ids


def urls(blocks):
    """(block_id, plats, adress, etikett) för de spårade länkarna i
    blocken, i renderarens ordning (render.collect_links utan Företagets
    uppgifter: kartlänken i öppettiderna kommer inte med). blocks:
    blocklistan eller active_blocks(...)."""
    from .render import spots_for_blocks

    return [(s.block_id, s.position, s.url, s.label) for s in spots_for_blocks(as_blocks(blocks))]


def terms_from(blocks):
    """Villkoren ur erbjudandeblocken (aktiva versioner), som kunden
    bekräftar med "Uppgifterna stämmer" (F.7): [{"label", "value"}]."""
    from .render import date_long

    out = []
    for block in blocks or []:
        if not isinstance(block, dict) or block.get("type") != "offer":
            continue
        fields = active_fields(block)
        rows = (
            ("Erbjudande", fields.get("title") or ""),
            ("Villkor", fields.get("text") or ""),
            (
                "Erbjudandet gäller",
                f"till {date_long(fields.get('valid_until'))}" if fields.get("valid_until") else "",
            ),
            ("Kod", fields.get("code") or ""),
        )
        for label, value in rows:
            item = {"label": label, "value": value}
            if value and item not in out:
                out.append(item)
    return out


# ---------------------------------------------------------------------------
# Valideringen och sparningen
# ---------------------------------------------------------------------------


def _clean_version(version, vwhere, block_id, block_type, cleaner_args, seen):
    """(rensad version, fel, adresser)."""
    errors = []
    if not isinstance(version, dict):
        return None, [_error(block_id, "", vwhere, "Versionen ska vara ett dict.")], []
    unknown = set(version) - pb.VERSION_KEYS
    if unknown:
        errors.append(_error(block_id, "", vwhere, f"Okända nycklar {', '.join(sorted(unknown))}."))
    version_id = version.get("id")
    if not isinstance(version_id, str) or not pb.VERSION_ID.fullmatch(version_id):
        errors.append(_error(block_id, "", vwhere, "Versionens id ska vara v_ och tolv tecken."))
    elif version_id in seen:
        errors.append(_error(block_id, "", vwhere, "Samma id som en annan version."))
    if version.get("source") not in pb.SOURCES:
        errors.append(_error(block_id, "", vwhere, "Okänd källa."))
    by = version.get("by")
    if by is not None and (isinstance(by, bool) or not isinstance(by, int) or by < 1):
        errors.append(_error(block_id, "", vwhere, "by ska vara en användares id eller null."))
    if not pb._valid_iso(version.get("at")):
        errors.append(_error(block_id, "", vwhere, "at ska vara en tid i ISO 8601."))
    cleaner = _Cleaner(**cleaner_args)
    fields = cleaner.fields(block_type, version.get("fields") or {}, block_id, vwhere)
    errors.extend(cleaner.errors)
    if errors:
        return None, errors, []
    clean = {
        "id": version_id,
        "fields": fields,
        "source": version.get("source"),
        "by": by,
        "at": version.get("at"),
    }
    sig = version.get("sig")
    if isinstance(sig, str) and pb.SIGNATURE.fullmatch(sig):
        clean["sig"] = sig
    return clean, [], cleaner.urls


def _validate(account, utskick, blocks):
    """(rensade block, de aktiva versionernas http(s)-adresser)."""
    from apps.flamingo.models import MediaAsset

    from .. import access

    if not isinstance(blocks, list):
        raise BlockError("Blocken ska vara en lista.")
    errors = []
    if len(blocks) > MAX_BLOCKS:
        errors.append(
            _error("", "", "", f"Högst {MAX_BLOCKS} block i ett mejl (nu {len(blocks)}).")
        )
    # Bilderna i de aktiva versionerna: kontots, annars 400 (H.1). En äldre
    # version med en bild som inte finns följer inte med (som sidornas).
    access.owned_ids(MediaAsset, account, _active_media_ids(blocks))
    wanted = [i for i in media_ids([b for b in blocks if isinstance(b, dict)])]
    owned = set(
        MediaAsset.objects.filter(account=account, pk__in=wanted).values_list("pk", flat=True)
    )
    cleaner_args = {
        "account": account,
        "media_ids": owned,
        "field_keys": account_field_keys(account),
    }
    stored_ids = {b.get("id") for b in doc_blocks(utskick)} if utskick is not None else set()
    state = registry.available(account, utskick) if utskick is not None else {}
    out, seen_blocks, urls_out = [], set(), []
    for n, raw in enumerate(blocks, start=1):
        where = f"Block {n}"
        if not isinstance(raw, dict):
            errors.append(_error("", "", where, "Blocket ska vara ett dict."))
            continue
        block_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
        unknown = set(raw) - pb.BLOCK_KEYS
        if unknown:
            errors.append(
                _error(block_id, "", where, f"Okända nycklar {', '.join(sorted(unknown))}.")
            )
        block_type = EMAIL_TYPES.get(raw.get("type"))
        if block_type is None:
            errors.append(_error(block_id, "", where, f"Okänd blocktyp {raw.get('type')!r}."))
            continue
        where = f"Block {n} ({block_type.name})"
        if not block_id or not pb.BLOCK_ID.fullmatch(block_id):
            errors.append(_error(block_id, "", where, "Blockets id ska vara b_ och tolv tecken."))
        elif block_id in seen_blocks:
            errors.append(_error(block_id, "", where, "Samma id som ett annat block."))
        seen_blocks.add(block_id)
        variant = raw.get("variant") or VARIANT
        if block_type.variant(variant) is None:
            errors.append(_error(block_id, "", where, f"Okänd variant {variant!r}."))
        ok, why = state.get(block_type.key, (True, ""))
        if not ok and block_id not in stored_ids:
            errors.append(_error(block_id, "", where, why))
        versions = raw.get("versions")
        if not isinstance(versions, list) or not versions:
            errors.append(_error(block_id, "", where, "Minst en version behövs."))
            continue
        if len(versions) > pb.MAX_VERSIONS:
            errors.append(
                _error(
                    block_id, "", where, f"Högst {pb.MAX_VERSIONS} versioner (nu {len(versions)})."
                )
            )
        active_id = raw.get("active")
        clean_versions, seen_versions = [], set()
        for m, version in enumerate(versions, start=1):
            vwhere = where if len(versions) == 1 else f"{where}, version {m}"
            clean, verrors, found = _clean_version(
                version, vwhere, block_id, block_type, cleaner_args, seen_versions
            )
            is_active = isinstance(version, dict) and version.get("id") == active_id
            if verrors:
                if is_active:
                    errors.extend(verrors)
                continue
            if is_active:
                urls_out.extend(found)
            seen_versions.add(clean["id"])
            clean_versions.append(clean)
        if not any(isinstance(v, dict) and v.get("id") == active_id for v in versions):
            errors.append(_error(block_id, "", where, "Den aktiva versionen finns inte."))
        out.append(
            {
                "id": block_id,
                "type": block_type.key,
                "variant": variant,
                "active": active_id,
                "versions": clean_versions,
            }
        )
    if errors:
        raise BlockError(errors)
    return out, urls_out


def validate(account, utskick, blocks):
    """Blocken rensade (se modulens beskrivning). Kastar BlockError med alla
    fel och access.ForeignIds för en bild som inte är kontots."""
    return _validate(account, utskick, blocks)[0]


def live_fields(type_key, fields):
    """Fälten utan de borttagna (registry.RETIRED_FIELDS). Redigeraren läser
    inte om blocken efter en sparning, så den skickar ett borttaget fält så
    länge sidan är öppen; det får inte göra versionen till en ny, varken här
    (_stamp) eller i redigerarens sparning (app_views/brev.py, _stamp och
    _unsigned)."""
    retired = registry.RETIRED_FIELDS.get(type_key)
    if not retired or not isinstance(fields, dict):
        return fields
    return {key: value for key, value in fields.items() if key not in retired}


def _stamp(stored_blocks, blocks, user, now=None):
    """Vem som skrev varje version, avgjort av servern (som sidornas
    app_views/pages.stamp_authorship): en sparad version med samma fält
    (borttagna fält räknas inte, live_fields) behåller källa, by och at;
    en ny version med e-postens signatur (mallen, AI) står som den är; allt
    annat får den inloggade (kunden, eller ADX för byrån i kundvyn)."""
    stored = {}
    for block in stored_blocks:
        for version in block.get("versions") or []:
            if isinstance(version, dict) and isinstance(version.get("id"), str):
                stored[version["id"]] = version
    by = _user_id(user)
    at = _now_iso(now)
    source = SOURCE_ADX if getattr(user, "is_staff", False) else SOURCE_CUSTOMER
    out = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or not isinstance(block.get("versions"), list):
            out.append(block)
            continue
        block = dict(block)
        type_key = block.get("type")
        versions = []
        for version in block["versions"]:
            if not isinstance(version, dict):
                versions.append(version)
                continue
            version = dict(version)
            before = stored.get(version.get("id")) if isinstance(version.get("id"), str) else None
            if before is not None and live_fields(type_key, before.get("fields")) == live_fields(
                type_key, version.get("fields")
            ):
                for key in ("source", "by", "at"):
                    version[key] = before.get(key)
            elif before is None and is_signed(version):
                pass
            else:
                version["by"], version["at"], version["source"] = by, at, source
            version.pop("sig", None)
            versions.append(version)
        block["versions"] = versions
        out.append(block)
    return out if isinstance(blocks, list) else blocks


def save(utskick, blocks, *, rev, user=None, account=None, now=None, terms_ok=False):
    """Spara blocken om ingen annan hunnit före (se modulens beskrivning).
    Returnerar det nya email_rev. Kastar BlockError, access.ForeignIds och
    StaleRevision."""
    from .. import links
    from ..models import Utskick

    account = account or utskick.account
    now = now or timezone.now()
    stored = doc_blocks(utskick)
    clean, found = _validate(account, utskick, _stamp(stored, copy.deepcopy(blocks), user, now))
    pb.sign_blocks(clean, salt=SIGN_SALT)
    terms = terms_from(clean)
    previous = utskick.confirmed_terms if isinstance(utskick.confirmed_terms, list) else []
    values = {
        "email_doc": {"blocks": clean},
        "email_rev": F("email_rev") + 1,
        "confirmed_terms": terms,
        "updated_at": now,
    }
    if terms != previous:
        values.update(terms_confirmed_at=None, terms_confirmed_by=None)
    if terms_ok and terms and getattr(user, "is_authenticated", False):
        values.update(terms_confirmed_at=now, terms_confirmed_by=user)
    updated = Utskick.objects.filter(
        pk=utskick.pk, email_rev=rev, status__in=Utskick.EDITABLE
    ).update(**values)
    if not updated:
        row = Utskick.objects.filter(pk=utskick.pk).values("email_rev", "status").first()
        if row is not None and row["status"] not in Utskick.EDITABLE:
            raise BlockError(LOCKED_TEXT)
        raise StaleRevision(row["email_rev"] if row else None)
    utskick.email_doc = {"blocks": clean}
    utskick.email_rev = rev + 1
    utskick.confirmed_terms = terms
    if "terms_confirmed_at" in values:
        utskick.terms_confirmed_at = values["terms_confirmed_at"]
        utskick.terms_confirmed_by = values["terms_confirmed_by"]
    utskick.updated_at = now
    # Nya värdar väntar på ADX (E.8): raden och byråns larm, efter
    # sparningen och utanför varje lås (larmet är ett mejl till byrån).
    if not getattr(account, "is_demo", False):
        requester = user if getattr(user, "is_authenticated", False) else utskick.created_by
        for url in dict.fromkeys(found):
            try:
                links.request_if_new(account, url, requester)
            except links.LinkRefused:
                continue
    return utskick.email_rev
