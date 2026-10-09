"""
Sms-texten i ett utskick (README F.3, H.5, I.8 och I.10): platshållarna,
kontrollen av texten, sammanfogningen, den färdiga texten per mottagare,
förhandsvisningen med räknaren och avregistreringsraden.

    placeholders(body) -> Placeholders(tags, links, unsubscribe, unknown)
    validate(account, body, utskick=None) -> list[str]
                                    okända platshållare, fält och länkar som inte
                                    finns, adresser skrivna direkt i texten
    merge_values(contact, defs=None) -> dict
                                    värdena som fryses på Recipient.merge:
                                    {"förnamn", "efternamn", "namn", "företag",
                                     "fält:<nyckel>"}
    merge(text, values, fallbacks) -> str
                                    {förnamn|du}, en rad, högst 60 tecken per värde
    render_sms(utskick, recipient, sender) -> str
                                    den frysta mottagarens text: värden, koderna
                                    (k.adx.se/<kod>) och avregistreringsraden
    render_test_sms(utskick, address, values, sender) -> str
                                    testsändningens text (F.8): riktiga koder utan mottagare
    preview(utskick, contact=None, sender=None, body=None) -> dict
                                    {"text", "parts", "encoding", "chars", "max_chars",
                                     "non_gsm", "longest_parts", "longest_count",
                                     "cost_ore", "part_ore", "counter", "sender"}
    opt_out_line(sender_kind, person_code=None) -> str
                                    "Svara STOPP för att inte få fler sms." eller
                                    "Avregistrera: k.adx.se/s/<kod>"
    sender_for_kind(utskick) -> str svarsnumret eller avsändarnamnet
    gsm_fix(text) -> str            "Byt automatiskt": tecken som gör sms:et dyrare
    TEMPLATES, template_body(key, display_name)   de inbyggda mallarna (I.10)

Platshållarna (en sluten lista, som website/templatetags/render_context.py):
{förnamn}, {efternamn}, {namn}, {företag} (kontaktens företag), {fält:<nyckel>}
(extrafälten), med reservtext i klamrarna ({förnamn|du}) eller i
Utskick.merge_fallbacks. Bara i sms: {länk:<nyckel>} (en TrackedLink i
utskicket, skrivs som k.adx.se/Ab12Cd) och {avregistrering} (raden placeras
där i stället för sist). En okänd platshållare är ett fel, inte tom text.

Avregistreringen (H.5): med svarsnumret läggs "Svara STOPP för att inte få
fler sms." till sist när texten inte redan säger det; med ett avsändarnamn
(eller när svarsnumret byts mot namnet vid en kollision, D.4) går det inte
att svara, så meningen om STOPP tas bort och "Avregistrera: k.adx.se/s/<kod>"
läggs till. Raden finns i varje utskick, information också (H.5 kräver den
när fler än en får sms:et, och den skadar aldrig för en).

Koderna: frysningen (sending/freeze.py, D.3) skapar klick- och personkoderna i
bulk. render_sms skapar en kod som ändå saknas (samma regler: unik kod, nytt
försök vid krock), så att en mottagare aldrig får en trasig länk.
"""

import math
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.common.security import normalize_typography
from apps.sms import encoding

from . import codes, keys
from .models import CHANNEL_SMS, FieldDef, LinkCode, TrackedLink, Utskick

#: Platshållarna för kontaktens värden (F.3). "fält:<nyckel>" kommer till.
TAG_NAMES = ("förnamn", "efternamn", "namn", "företag")
FIELD_PREFIX = "fält:"
LINK_PREFIX = "länk:"
UNSUBSCRIBE = "avregistrering"
#: Varje {...} i texten; vad den är avgörs av _classify.
TOKEN_RE = re.compile(r"\{([^{}\n]{1,80})\}")
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
_LINK_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
#: Adresser skrivna direkt i texten: de spåras inte och prövas inte (E.8).
RAW_URL_RE = re.compile(r"https?://|\bwww\.", re.I)
#: Kunden har redan skrivit om STOPP (med svarsnumret behövs då ingen rad till).
STOP_RE = re.compile(r"svara\s+stopp", re.I)

MAX_BODY = 1000
#: Högsta antal delar efter sammanfogningen (apps.sms.service.MAX_PARTS).
MAX_PARTS = 6
#: Värdena i ett sms: en rad, högst så här långa (F.3).
VALUE_MAX = 60
#: Exempelkoden i förhandsvisningen och räknaren: lika lång som en riktig.
SAMPLE_CODE = "a8Kf2X"

#: Hårt blanksteg: "0,39 kr" bryts aldrig.
NBSP = chr(0xA0)
STOP_LINE = "Svara STOPP för att inte få fler sms."
UNSUBSCRIBE_LABEL = "Avregistrera: "

#: 46elks pris per del till Sverige när ingen historik finns (provkörning
#: 2026-10-03: 5200 per del, apps/sms/README.md), i tiotusendels krona.
#: Bara för uppskattningen i gränssnittet; sändningen prissätts av apps/sms.
ESTIMATE_PART_UNITS = 5200
#: Påslaget när kunden saknar sms-konto (apps.sms.models.DEFAULT_MARKUP_ORE).
ESTIMATE_MARKUP_ORE = 5
#: Så många kontakter läses för "Längsta namnet ger 2 delar för 6 personer".
LONGEST_SCAN = 25_000

#: "Byt automatiskt" (I.6): tecken som gör sms:et till UCS-2 men har en
#: vanlig motsvarighet. Tankstreck, typografiska citattecken och ellipsen
#: byts av normalize_typography; resten här.
#: Kodpunkterna står som tal: osynliga tecken hör inte hemma i källkoden.
_SPACES = (0x00A0, 0x2002, 0x2003, 0x2009, 0x202F, 0x0009)
_INVISIBLE = (0x200B, 0xFEFF)
_HYPHENS = (0x2010, 0x2011, 0x2012, 0x2212, 0x2022, 0x00B7)
_APOSTROPHES = (0x00B4, 0x0060, 0x2032, 0x201B)
_QUOTES = (0x00AB, 0x00BB, 0x2033)
_GSM_FIX = {
    **{cp: " " for cp in _SPACES},
    **{cp: None for cp in _INVISIBLE},
    **{cp: "-" for cp in _HYPHENS},
    **{cp: "'" for cp in _APOSTROPHES},
    **{cp: '"' for cp in _QUOTES},
}

#: De inbyggda mallarna (I.10). "D" ersätts med företagsnamnet när mallen
#: används. purpose och info_reason sätts på utskicket; reply betyder att
#: mallen frågar efter svar (svarsnumret väljs).
TEMPLATES = (
    {
        "key": "paminnelse",
        "label": "Påminnelse",
        "purpose": "reklam",
        "info_reason": "",
        "reply": False,
        "body": "Hej {förnamn|du}, det är dags för service igen. "
        "Boka en tid som passar dig: {länk:boka} /D",
    },
    {
        "key": "erbjudande",
        "label": "Erbjudande",
        "purpose": "reklam",
        "info_reason": "",
        "reply": False,
        "body": "Hej {förnamn|du}, D har ett erbjudande till dig som är kund. "
        "Läs mer: {länk:erbjudande}",
    },
    {
        "key": "oppettider",
        "label": "Nya öppettider",
        "purpose": "information",
        "info_reason": "oppettider",
        "reply": False,
        "body": "Hej, D har nya öppettider från måndag: vardagar 8 till 17. Välkommen.",
    },
    {
        "key": "fraga",
        "label": "Fråga",
        "purpose": "reklam",
        "info_reason": "",
        "reply": True,
        "body": "Hej {förnamn|du}, vill du att D ringer upp dig om en tid för service? "
        "Svara JA i så fall.",
    },
)


def template_for(key):
    return next((t for t in TEMPLATES if t["key"] == key), None)


def template_body(key, display_name):
    """Mallens text med företagsnamnet insatt ("D" i I.10)."""
    template = template_for(key)
    if template is None:
        return ""
    name = display_name or "oss"
    return re.sub(r"\bD\b", lambda _match: name, template["body"])


# ---------------------------------------------------------------------------
# Platshållarna
# ---------------------------------------------------------------------------


@dataclass
class Placeholders:
    #: Värdenas namn i ordning, utan dubbletter: "förnamn", "fält:regnummer".
    tags: list = field(default_factory=list)
    #: Länkarnas nycklar i ordning, utan dubbletter.
    links: list = field(default_factory=list)
    #: {avregistrering} står i texten (antal gånger).
    unsubscribe: int = 0
    #: Det som stod i klamrar men inte är en platshållare.
    unknown: list = field(default_factory=list)

    @property
    def fields(self):
        return [t[len(FIELD_PREFIX) :] for t in self.tags if t.startswith(FIELD_PREFIX)]


def _classify(inner):
    """(sort, namn, reservtext) för det som står i klamrarna: sort är
    "tag", "link", "unsubscribe" eller "" (okänt)."""
    name, bar, fallback = inner.partition("|")
    name = name.strip()
    lowered = name.lower()
    if not bar and lowered == UNSUBSCRIBE:
        return "unsubscribe", UNSUBSCRIBE, ""
    if not bar and lowered.startswith(LINK_PREFIX):
        key = name[len(LINK_PREFIX) :].strip()
        if _LINK_KEY_RE.fullmatch(key):
            return "link", key, ""
        return "", name, ""
    if lowered in TAG_NAMES:
        return "tag", lowered, fallback
    if lowered.startswith(FIELD_PREFIX):
        key = name[len(FIELD_PREFIX) :].strip()
        if _KEY_RE.fullmatch(key):
            return "tag", FIELD_PREFIX + key, fallback
    return "", name, ""


def placeholders(body):
    found = Placeholders()
    for match in TOKEN_RE.finditer(body or ""):
        kind, name, _fallback = _classify(match.group(1))
        if kind == "tag" and name not in found.tags:
            found.tags.append(name)
        elif kind == "link" and name not in found.links:
            found.links.append(name)
        elif kind == "unsubscribe":
            found.unsubscribe += 1
        elif not kind and match.group(0) not in found.unknown:
            found.unknown.append(match.group(0))
    return found


def validate(account, body, utskick=None):
    """Fel i texten som svensk text, tom lista när den går att skicka.
    Med utskick prövas också att varje {länk:...} finns i utskicket.
    Längden efter sammanfogningen (högst sex delar) prövar vyn med
    preview(), som känner mottagarnas värden."""
    text = body or ""
    errors = []
    if not text.strip():
        return ["Skriv texten i sms:et."]
    if len(text) > MAX_BODY:
        errors.append(f"Texten får vara högst 1{NBSP}000 tecken.")
    found = placeholders(text)
    for token in found.unknown:
        errors.append(
            f"{token} är ingen platshållare. Använd {{förnamn}}, {{efternamn}}, {{namn}}, "
            "{företag}, {fält:...} eller {länk:...}, eller ta bort klamrarna."
        )
    if found.fields:
        known = set(
            FieldDef.objects.filter(account=account, key__in=found.fields).values_list(
                "key", flat=True
            )
        )
        for key in found.fields:
            if key not in known:
                errors.append(
                    f"Fältet {{fält:{key}}} finns inte. Skapa det under Kontakter, Fält, "
                    "eller ta bort det ur texten."
                )
    if utskick is not None and found.links:
        known = set(
            TrackedLink.objects.filter(utskick=utskick, key__in=found.links).values_list(
                "key", flat=True
            )
        )
        for key in found.links:
            if key not in known:
                errors.append(
                    f"Länken {{länk:{key}}} finns inte. Lägg till den under Länkar i Innehåll."
                )
    if found.unsubscribe > 1:
        errors.append("Skriv {avregistrering} högst en gång.")
    if RAW_URL_RE.search(TOKEN_RE.sub("", text)):
        errors.append(
            "Skriv inte adresser direkt i texten. Lägg till länken under Länkar, så att den "
            "spåras och kontrolleras."
        )
    return errors


# ---------------------------------------------------------------------------
# Sammanfogningen
# ---------------------------------------------------------------------------

_MONTHS = ("jan", "feb", "mars", "april", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec")


def _one_line(value):
    return " ".join(str(value or "").split())[:VALUE_MAX]


def _field_text(definition, value):
    """Ett extrafälts värde som det står i ett sms: datum som "4 nov 2025"."""
    if definition is not None and definition.kind == FieldDef.Kind.DATE and value:
        try:
            year, month, day = (int(p) for p in str(value).split("-"))
            return f"{day} {_MONTHS[month - 1]} {year}"
        except (ValueError, IndexError):
            return str(value)
    return str(value)


def field_defs(account):
    """Kontots extrafält som {nyckel: FieldDef} (merge_values datum)."""
    return {d.key: d for d in FieldDef.objects.filter(account=account)}


def merge_values(contact, defs=None):
    """Kontaktens värden för sammanfogningen, som frysningen sparar på
    Recipient.merge. Tomma värden utelämnas (reservtexten gäller då)."""
    if contact is None:
        return {}
    values = {
        "förnamn": contact.first_name,
        "efternamn": contact.last_name,
        "namn": contact.full_name or contact.company_name,
        "företag": contact.company_name,
    }
    for key, value in (contact.fields or {}).items():
        if value not in (None, ""):
            definition = (defs or {}).get(key)
            values[FIELD_PREFIX + key] = _field_text(definition, value)
    return {name: _one_line(value) for name, value in values.items() if _one_line(value)}


def merge(text, values, fallbacks):
    """Platshållarna för värden ersatta: värdet, annars reservtexten i
    klamrarna, annars utskickets reservtext, annars inget. {länk:...},
    {avregistrering} och okända klamrar lämnas kvar."""
    values = values or {}
    fallbacks = fallbacks or {}

    def replace(match):
        kind, name, inline = _classify(match.group(1))
        if kind != "tag":
            return match.group(0)
        value = _one_line(values.get(name))
        if value:
            return value
        if inline.strip():
            return _one_line(inline)
        return _one_line(fallbacks.get(name))

    return TOKEN_RE.sub(replace, text or "")


# ---------------------------------------------------------------------------
# Avsändaren och avregistreringen
# ---------------------------------------------------------------------------


def reply_number():
    return str(getattr(settings, "UTSKICK_REPLY_NUMBER", "") or "+46766860046").strip()


def sender_for_kind(utskick):
    """Avsändaren utskicket valt: svarsnumret, eller avsändarnamnet."""
    if utskick.sms_sender_kind == Utskick.SenderKind.NAME and utskick.sms_sender_name:
        return utskick.sms_sender_name
    return reply_number()


def is_reply_sender(sender):
    return str(sender or "") == reply_number()


def opt_out_line(sender_kind, person_code=None):
    """Raden som avslutar sms:et (H.5). sender_kind är "reply" eller "name"
    (Utskick.SenderKind); utan kod visas exempelkoden."""
    if sender_kind == Utskick.SenderKind.REPLY:
        return STOP_LINE
    from .links import sms_link

    return UNSUBSCRIBE_LABEL + sms_link(person_code or SAMPLE_CODE, "s")


def _with_opt_out(text, sender, person_code):
    reply = is_reply_sender(sender)
    line = opt_out_line(Utskick.SenderKind.REPLY if reply else Utskick.SenderKind.NAME, person_code)
    marker = "{" + UNSUBSCRIBE + "}"
    if not reply:
        # Ett namn går inte att svara till: meningen om STOPP vilseleder.
        text = re.sub(r"\s*" + re.escape(STOP_LINE), "", text)
    if marker in text:
        return text.replace(marker, line).strip()
    if reply and STOP_RE.search(text):
        return text.strip()
    return f"{text.rstrip()}\n{line}"


def _compose(body, values, fallbacks, sender, link_text, person_code):
    text = merge(body, values, fallbacks)

    def replace(match):
        kind, key, _fallback = _classify(match.group(1))
        if kind != "link":
            return match.group(0)
        return link_text(key)

    text = TOKEN_RE.sub(replace, text)
    return _with_opt_out(text, sender, person_code)


# ---------------------------------------------------------------------------
# Den färdiga texten
# ---------------------------------------------------------------------------


def _create_code(*, kind, account, value_hash, recipient=None, link=None):
    """En ny kod med samma regler som frysningen: unik, nytt försök vid krock."""
    for _ in range(codes.MAX_TRIES):
        try:
            with transaction.atomic():
                return LinkCode.objects.create(
                    code=codes.new_code(),
                    kind=kind,
                    account=account,
                    channel=CHANNEL_SMS,
                    value_hash=value_hash,
                    recipient=recipient,
                    link=link,
                )
        except IntegrityError:
            if recipient is not None:
                found = LinkCode.objects.filter(kind=kind, recipient=recipient, link=link).first()
                if found is not None:
                    return found
    raise codes.CodeCollision("Ingen ledig sms-kod.")


def _links_by_key(utskick, keys_wanted):
    rows = TrackedLink.objects.filter(utskick=utskick, key__in=list(keys_wanted))
    return {row.key: row for row in rows}


def render_sms(utskick, recipient, sender):
    """Texten mottagaren får: de frysta värdena, en kod per länk och
    avregistreringsraden för avsändaren som faktiskt används (D.4)."""
    from .links import sms_link

    body = utskick.sms_body
    found = placeholders(body)
    value_hash = keys.value_hash(CHANNEL_SMS, recipient.address)
    link_rows = _links_by_key(utskick, found.links)
    missing = [key for key in found.links if key not in link_rows]
    if missing:
        raise ValueError(f"Utskick {utskick.pk} saknar en länk i texten.")
    existing = {
        row.link_id: row.code
        for row in LinkCode.objects.filter(
            recipient=recipient, kind=LinkCode.Kind.LINK, link__in=list(link_rows.values())
        )
    }
    by_key = {}
    for key, link in link_rows.items():
        code = existing.get(link.pk)
        if code is None:
            code = _create_code(
                kind=LinkCode.Kind.LINK,
                account=utskick.account,
                value_hash=value_hash,
                recipient=recipient,
                link=link,
            ).code
        by_key[key] = code
    person_code = None
    if not is_reply_sender(sender):
        row = LinkCode.objects.filter(recipient=recipient, kind=LinkCode.Kind.PERSON).first()
        if row is None:
            row = _create_code(
                kind=LinkCode.Kind.PERSON,
                account=utskick.account,
                value_hash=value_hash,
                recipient=recipient,
            )
        person_code = row.code
    return _compose(
        body,
        recipient.merge,
        utskick.merge_fallbacks,
        sender,
        lambda key: sms_link(by_key[key]),
        person_code,
    )


def render_test_sms(utskick, address, values, sender):
    """Testsändningens text (F.8): som en mottagares, med värdena från den
    valda kontakten och riktiga koder utan mottagare (klicket leder till
    målet men räknas inte, /s/ gäller testnumret)."""
    from .links import sms_link

    body = utskick.sms_body
    found = placeholders(body)
    value_hash = keys.value_hash(CHANNEL_SMS, address)
    link_rows = _links_by_key(utskick, found.links)
    by_key = {}
    for key in found.links:
        link = link_rows.get(key)
        if link is None:
            raise ValueError(f"Utskick {utskick.pk} saknar en länk i texten.")
        by_key[key] = _create_code(
            kind=LinkCode.Kind.LINK, account=utskick.account, value_hash=value_hash, link=link
        ).code
    person_code = None
    if not is_reply_sender(sender):
        person_code = _create_code(
            kind=LinkCode.Kind.PERSON, account=utskick.account, value_hash=value_hash
        ).code
    return _compose(
        body,
        values,
        utskick.merge_fallbacks,
        sender,
        lambda key: sms_link(by_key[key]),
        person_code,
    )


# ---------------------------------------------------------------------------
# Förhandsvisningen och räknaren
# ---------------------------------------------------------------------------


def part_units(sms_account=None, now=None):
    """Uppskattat pris per del till Sverige i tiotusendels krona, påslaget
    inräknat: de senaste riktiga sms:en, annars ESTIMATE_PART_UNITS."""
    from apps.sms import pricing
    from apps.sms.models import UNITS_PER_ORE

    provider = pricing.recent_part_cost("SE", now) or ESTIMATE_PART_UNITS
    if sms_account is not None:
        markup = sms_account.markup_units_per_part
    else:
        markup = ESTIMATE_MARKUP_ORE * UNITS_PER_ORE
    return int(provider) + int(markup)


def units_to_ore(units):
    """Tiotusendels krona till hela öre, avrundat uppåt (hellre för högt)."""
    return int(math.ceil(int(units or 0) / 100))


def ore_text(ore):
    """39 -> "0,39 kr", 151 -> "1,51 kr"."""
    kr, rest = divmod(int(ore), 100)
    return f"{kr},{rest:02d}{NBSP}kr"


def parts_word(parts):
    return f"{parts} del" if parts == 1 else f"{parts} delar"


def counter_text(analysis):
    """ "GSM-7 · 134 av 160 · 1 del" (README I.2), som räknaren i
    flamingo-app-utskick.js skriver det."""
    label = "GSM-7" if analysis.encoding == encoding.GSM7 else "UCS-2"
    parts = max(analysis.parts, 1)
    room = encoding.max_length(analysis.encoding, parts)
    return f"{label} · {analysis.units} av {room} · {parts_word(analysis.parts)}"


def body_non_gsm(body):
    """Tecken i kundens egen text (utan platshållarna) som inte finns i
    GSM-7, i ordning, högst tio."""
    odd = []
    for ch in TOKEN_RE.sub("", body or ""):
        if ch not in encoding.GSM_BASIC and ch not in encoding.GSM_EXTENDED and ch not in odd:
            odd.append(ch)
            if len(odd) == 10:
                break
    return odd


#: Tecken som inte syns, eller som liknar ett vanligt tecken, med namn i
#: varningen "Texten innehåller ..." (kodpunkter: inga osynliga tecken i källan).
_CHAR_NAMES = {
    0x00A0: "ett hårt mellanslag",
    0x2002: "ett brett mellanslag",
    0x2003: "ett brett mellanslag",
    0x2009: "ett smalt mellanslag",
    0x202F: "ett smalt mellanslag",
    0x0009: "en tabb",
    0x200B: "ett osynligt tecken",
    0xFEFF: "ett osynligt tecken",
    0x2018: "en typografisk apostrof",
    0x2019: "en typografisk apostrof",
    0x201C: "ett typografiskt citattecken",
    0x201D: "ett typografiskt citattecken",
    0x2013: "ett tankstreck",
    0x2014: "ett tankstreck",
    0x2026: "tre punkter i ett tecken",
}
#: De som inte syns alls skrivs bara med namn.
_UNSEEN = frozenset((*_SPACES, *_INVISIBLE))


def non_gsm_text(chars):
    """Varningens tecken i klartext: "ett hårt mellanslag", eller tecknet med
    namn inom parentes som i skissen. Andra tecken (emoji, bokstäver utanför
    GSM-7) står som de är. Delarna skiljs med kommatecken."""
    parts = []
    for ch in chars or ():
        name = _CHAR_NAMES.get(ord(ch))
        if name and ord(ch) in _UNSEEN:
            text = name
        elif name:
            text = f"{ch} ({name})"
        else:
            text = ch
        if text not in parts:
            parts.append(text)
    return ", ".join(parts)


def gsm_fix(text):
    """Texten med de vanliga dyra tecknen utbytta: typografiska citattecken
    och streck, ellipsen, hårda och smala mellanslag, punkter och accenter.
    Emojis och bokstäver utanför GSM-7 lämnas (de listas i stället)."""
    return normalize_typography(text or "").translate(_GSM_FIX)


def _audience_value_rows(utskick, tags):
    """(antal, värden) för mottagarnas olika kombinationer av de värden
    texten använder, högst LONGEST_SCAN kontakter."""
    from . import audience

    defs = field_defs(utskick.account) if any(t.startswith(FIELD_PREFIX) for t in tags) else {}
    rows = audience.contacts(utskick).values_list(
        "first_name", "last_name", "company_name", "fields"
    )[:LONGEST_SCAN]
    seen = {}
    for first, last, company, fields in rows:
        full = " ".join(p for p in (first, last) if p)
        values = {"förnamn": first, "efternamn": last, "namn": full or company, "företag": company}
        for tag in tags:
            if tag.startswith(FIELD_PREFIX):
                key = tag[len(FIELD_PREFIX) :]
                raw = (fields or {}).get(key)
                if raw not in (None, ""):
                    values[tag] = _field_text(defs.get(key), raw)
        picked = tuple(_one_line(values.get(tag)) for tag in tags)
        seen[picked] = seen.get(picked, 0) + 1
    return seen


def preview(utskick, contact=None, sender=None, body=None, with_longest=True, sms_account=None):
    """Förhandsvisningen och räknarens siffror. body ersätter utskickets
    sparade text (redigeraren skickar den osparade texten). Länkarna visas
    med exempelkoden, som är lika lång som en riktig.

    longest_parts och longest_count: flest delar någon mottagare får med
    sina egna värden, och hur många som får fler delar än förhandsvisningen
    ("Längsta namnet ger 2 delar för 6 personer"). Räknas bara när texten
    har värden och utskicket är sparat."""
    from .links import sms_link

    body = utskick.sms_body if body is None else body
    sender = sender or sender_for_kind(utskick)
    values = merge_values(contact, field_defs(utskick.account) if contact is not None else None)
    text = _compose(
        body,
        values,
        utskick.merge_fallbacks,
        sender,
        lambda _key: sms_link(SAMPLE_CODE),
        SAMPLE_CODE,
    )
    analysis = encoding.analyse(text)
    longest_parts, longest_count = analysis.parts, 0
    found = placeholders(body)
    if with_longest and found.tags and utskick.pk:
        for picked, count in _audience_value_rows(utskick, found.tags).items():
            rendered = _compose(
                body,
                dict(zip(found.tags, picked, strict=True)),
                utskick.merge_fallbacks,
                sender,
                lambda _key: sms_link(SAMPLE_CODE),
                SAMPLE_CODE,
            )
            parts = encoding.analyse(rendered).parts
            if parts > longest_parts:
                longest_parts = parts
            if parts > analysis.parts:
                longest_count += count
    per_part = part_units(sms_account)
    return {
        "text": text,
        "parts": analysis.parts,
        "encoding": analysis.encoding,
        "chars": analysis.units,
        "max_chars": encoding.max_length(analysis.encoding, max(analysis.parts, 1)),
        "non_gsm": body_non_gsm(body),
        "longest_parts": longest_parts,
        "longest_count": longest_count,
        "part_ore": units_to_ore(per_part),
        "cost_ore": units_to_ore(per_part * analysis.parts),
        "counter": counter_text(analysis),
        "sender": sender,
    }
