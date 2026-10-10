"""
Blocken som data: skapa, versioner, schemat och formuläret.

    new_block(typ, variant, account, *, user=None, source="template", ctx=None)
    add_version(block, fields, source, user, *, activate=True)
    activate_version(block, version_id)
    active_version(block), active_fields(block)
    validate_blocks(blocks, *, account=None) -> rensade block, eller BlockError
    form_spec(blocks) -> FormSpec eller None
    blocks_from_content(content, mode, account, *, ctx=None)
    sign_version(version), is_signed(version)
                                     serverns signatur på en version

types= (active_fields, visible_items) och salt= (sign_version, is_signed,
sign_blocks) är för utskickens Brev (apps/utskick/email, README F.1):
e-posten har egna blocktyper och ett eget salt, så att en version som
kopierats från en sida aldrig behåller en giltig signatur. Utan dem gäller
sidornas TYPES och salt, som förut.

Formen på ett block står i pagebuilder/__init__.py.

Varje version som servern skapar eller sparar får en signatur ("sig"): en
HMAC (nyckeln härledd ur SECRET_KEY) över versionens id, källa, by, at och
fälten. Redigeraren skickar tillbaka hela blocken; en version som servern
inte känner igen och som saknar en giltig signatur får den inloggades källa
(app_views/pages.stamp_authorship). Ingen kan alltså kalla sin egen text
"mallen" eller "AI", eller skriva en annan persons namn på en version.
"""

import copy
import hashlib
import json
import logging
import re
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.text import slugify

from apps.common.security import sanitize_multiline_text, sanitize_plain_text

from ..models import PAGE_QUESTION_KINDS, MediaAsset, Service
from . import registry
from .registry import (
    CHOICE,
    ITEMS,
    KEY,
    LINES,
    MEDIA,
    PHONE,
    TEXT,
    TEXTAREA,
    TYPES,
    BuildContext,
)

#: Varifrån en version kommer.
SOURCE_TEMPLATE = "template"
SOURCE_AI = "ai"
SOURCE_CUSTOMER = "customer"
SOURCE_ADX = "adx"
SOURCES = (SOURCE_TEMPLATE, SOURCE_AI, SOURCE_CUSTOMER, SOURCE_ADX)
SOURCE_LABELS = {
    SOURCE_TEMPLATE: "Mallen",
    SOURCE_AI: "AI",
    SOURCE_CUSTOMER: "Kunden",
    SOURCE_ADX: "ADX",
}

#: Högst så många block på en sida och versioner per block. add_version
#: tar bort de äldsta versionerna (aldrig den aktiva) när gränsen nås.
MAX_BLOCKS = 40
MAX_VERSIONS = 12

BLOCK_KEYS = frozenset({"id", "type", "variant", "active", "versions"})
VERSION_KEYS = frozenset({"id", "fields", "source", "by", "at", "sig"})
#: Prövas alltid med fullmatch (ett $ släpper igenom en radbrytning sist).
BLOCK_ID = re.compile(r"b_[A-Za-z0-9]{12}")
VERSION_ID = re.compile(r"v_[A-Za-z0-9]{12}")
QUESTION_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
SIGNATURE = re.compile(r"[0-9a-f]{32}")
_ALPHABET = string.ascii_letters + string.digits
_SIGN_SALT = "apps.flamingo.pagebuilder.version"

logger = logging.getLogger(__name__)


class BlockError(ValueError):
    """Blocken klarar inte schemat. errors är svenska texter med var felet
    sitter ("Block 2 (Toppen), Rubrik: ...")."""

    def __init__(self, errors):
        self.errors = list(errors) if isinstance(errors, (list, tuple)) else [str(errors)]
        super().__init__("; ".join(self.errors))


class BlockUnavailable(ValueError):
    """Blocket går inte att lägga till: en bekräftad uppgift saknas."""


def _random_id(prefix):
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(12))


def new_block_id():
    return _random_id("b_")


def new_version_id():
    return _random_id("v_")


def _user_id(user):
    pk = getattr(user, "pk", None)
    return pk if isinstance(pk, int) else None


def _now_iso(now=None):
    return (now or timezone.now()).isoformat()


def _version(fields, source, user, now=None):
    if source not in SOURCES:
        raise BlockError(f"Okänd källa för versionen: {source}")
    return sign_version(
        {
            "id": new_version_id(),
            "fields": fields,
            "source": source,
            "by": _user_id(user),
            "at": _now_iso(now),
        }
    )


# ---------------------------------------------------------------------------
# Signaturen: versionen som servern skapade eller sparade
# ---------------------------------------------------------------------------


def _signature(version, salt=None):
    fields = version.get("fields")
    digest = hashlib.sha256(
        json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    by = version.get("by")
    message = "|".join(
        [
            str(version.get("id") or ""),
            str(version.get("source") or ""),
            str(by) if isinstance(by, int) and not isinstance(by, bool) else "",
            str(version.get("at") or ""),
            digest,
        ]
    )
    return salted_hmac(salt or _SIGN_SALT, message, algorithm="sha256").hexdigest()[:32]


def sign_version(version, *, salt=None):
    """Serverns signatur på versionen (version["sig"]), över id, källa, by,
    at och fälten. Versionen ändras på plats och returneras. salt: ett eget
    salt (utskickens Brev); utan det sidornas."""
    version["sig"] = _signature(version, salt)
    return version


def is_signed(version, *, salt=None):
    """Har versionen serverns signatur, och är den oförändrad sedan dess?
    En version som signerats med ett annat salt räknas inte."""
    sig = version.get("sig") if isinstance(version, dict) else None
    if not isinstance(sig, str) or not SIGNATURE.fullmatch(sig):
        return False
    try:
        return constant_time_compare(sig, _signature(version, salt))
    except (TypeError, ValueError):
        return False


def sign_blocks(blocks, *, salt=None):
    """Signera varje version i blocken (blocken ändras på plats)."""
    for block in blocks:
        for version in block.get("versions") or []:
            if isinstance(version, dict):
                sign_version(version, salt=salt)
    return blocks


# ---------------------------------------------------------------------------
# Skapa och versioner
# ---------------------------------------------------------------------------


def new_block(type_key, variant, account, *, user=None, source=SOURCE_TEMPLATE, ctx=None, now=None):
    """Ett nytt block med en version: mallens innehåll ur kontots bekräftade
    uppgifter (registry.template_fields). variant None ger typens första.

    Kastar BlockUnavailable när blocket inte går att lägga till (pris,
    certifikat, garanti eller ringremsa utan bekräftad uppgift, område utan
    orter), och BlockError för en okänd typ eller variant."""
    block_type = TYPES.get(type_key)
    if block_type is None:
        raise BlockError(f"Okänd blocktyp: {type_key}")
    variant = variant or block_type.default_variant
    if block_type.variant(variant) is None:
        raise BlockError(f"{block_type.name} har ingen variant {variant}.")
    fields = registry.template_fields(type_key, variant, account, ctx=ctx)
    if fields is None:
        reason = registry.REQUIRES_TEXT.get(block_type.requires) or (
            "Det finns inget bekräftat att visa i blocket än."
        )
        raise BlockUnavailable(f"{block_type.name}: {reason}")
    fields = clean_fields(block_type, fields)
    version = _version(fields, source, user, now)
    return {
        "id": new_block_id(),
        "type": type_key,
        "variant": variant,
        "active": version["id"],
        "versions": [version],
    }


def add_version(block, fields, source, user, *, activate=True, now=None):
    """Lägg till en version med fälten (rensade mot schemat; BlockError om
    något inte går). Den nya blir aktiv om inte activate=False. Fler än
    MAX_VERSIONS: de äldsta som inte är aktiva tas bort. Returnerar
    versionen; blocket ändras på plats."""
    block_type = TYPES.get(block.get("type"))
    if block_type is None:
        raise BlockError(f"Okänd blocktyp: {block.get('type')}")
    version = _version(clean_fields(block_type, fields), source, user, now)
    block.setdefault("versions", []).append(version)
    if activate or not block.get("active"):
        block["active"] = version["id"]
    while len(block["versions"]) > MAX_VERSIONS:
        oldest = next(v for v in block["versions"] if v["id"] != block["active"])
        block["versions"].remove(oldest)
    return version


def activate_version(block, version_id):
    """Gör versionen aktiv. Kastar BlockError om blocket inte har den."""
    if not any(v.get("id") == version_id for v in block.get("versions") or []):
        raise BlockError("Versionen finns inte i blocket.")
    block["active"] = version_id
    return block


def active_version(block):
    versions = block.get("versions") or []
    active = block.get("active")
    for version in versions:
        if version.get("id") == active:
            return version
    return versions[-1] if versions else None


def active_fields(block, types=None):
    """Den aktiva versionens fält, med tomma värden för det som saknas.
    types: blocktyperna (standard sidornas TYPES)."""
    block_type = (TYPES if types is None else types).get(block.get("type"))
    version = active_version(block)
    fields = (version or {}).get("fields") or {}
    if block_type is None:
        return dict(fields)
    return {f.key: fields.get(f.key, f.empty()) for f in block_type.fields}


def visible_items(block, field_key, items, types=None):
    """Posterna varianten visar (Variant.limits), annars alla."""
    block_type = (TYPES if types is None else types).get(block.get("type"))
    variant = block_type.variant(block.get("variant")) if block_type else None
    limit = (variant.limits or {}).get(field_key) if variant else None
    return list(items)[:limit] if limit is not None else list(items)


# ---------------------------------------------------------------------------
# Schemat
# ---------------------------------------------------------------------------


def _plain(value, limit):
    return sanitize_plain_text(str(value), max_length=max(limit * 4, 1000))


def _multi(value, limit):
    return sanitize_multiline_text(str(value), max_length=max(limit * 4, 2000)).strip()


def _check_length(text, spec, where, errors):
    if spec.max_length and len(text) > spec.max_length:
        errors.append(f"{where}: högst {spec.max_length} tecken (nu {len(text)}).")


def _clean_value(spec, value, where, errors, media_ids):
    kind = spec.kind
    if kind in (TEXT, PHONE, TEXTAREA):
        if value is None:
            return ""
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            errors.append(f"{where}: ska vara text.")
            return ""
        text = (
            _multi(value, spec.max_length) if kind == TEXTAREA else _plain(value, spec.max_length)
        )
        _check_length(text, spec, where, errors)
        return text
    if kind == LINES:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            errors.append(f"{where}: ska vara en lista med rader.")
            return []
        lines = []
        for i, raw in enumerate(value):
            if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
                errors.append(f"{where}, rad {i + 1}: ska vara text.")
                continue
            line = _plain(raw, spec.max_length)
            if not line:
                continue
            _check_length(line, spec, f"{where}, rad {i + 1}", errors)
            lines.append(line)
        if spec.max_items and len(lines) > spec.max_items:
            errors.append(f"{where}: högst {spec.max_items} rader (nu {len(lines)}).")
        return lines
    if kind == MEDIA:
        if value in (None, ""):
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(f"{where}: ska vara en bild ur mediaarkivet.")
            return None
        if media_ids is not None and value not in media_ids:
            errors.append(f"{where}: bilden finns inte i kontots mediaarkiv.")
            return None
        return value
    if kind == CHOICE:
        values = [c[0] for c in spec.choices]
        if value in (None, ""):
            return values[0] if values else ""
        if value not in values:
            errors.append(f"{where}: välj ett av {', '.join(values)}.")
            return values[0] if values else ""
        return value
    if kind == KEY:
        text = str(value or "").strip()
        if text and not QUESTION_KEY.fullmatch(text):
            errors.append(f"{where}: bara a-z, 0-9, _ och bindestreck.")
        return text
    if kind == ITEMS:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            errors.append(f"{where}: ska vara en lista.")
            return []
        items = []
        for i, raw in enumerate(value):
            item_where = f"{where}, nummer {i + 1}"
            if not isinstance(raw, dict):
                errors.append(f"{item_where}: ska vara en post med fält.")
                continue
            unknown = set(raw) - {f.key for f in spec.items}
            if unknown:
                errors.append(f"{item_where}: okända fält {', '.join(sorted(unknown))}.")
            item = {}
            for sub in spec.items:
                item[sub.key] = _clean_value(
                    sub, raw.get(sub.key), f"{item_where}, {sub.label}", errors, media_ids
                )
            texts = [v for k, v in item.items() if spec.sub(k).kind in (TEXT, TEXTAREA, PHONE)]
            if not any(texts):
                continue
            items.append(item)
        if spec.max_items and len(items) > spec.max_items:
            errors.append(f"{where}: högst {spec.max_items} (nu {len(items)}).")
        if spec.sub("key") is not None:
            _fill_keys(items)
        return items
    errors.append(f"{where}: okänd fältsort {kind}.")
    return None


def _fill_keys(items):
    """Formulärets frågor: varje fråga har en unik nyckel. Saknas den skapas
    den ur etiketten ("Ungefär hur stort?" blir "ungefar-hur-stort").
    Lead.answers använder etiketten, inte nyckeln."""
    seen = set()
    for n, item in enumerate(items, start=1):
        key = item.get("key") or slugify(item.get("label") or "")[:40].strip("-_")
        key = key or f"fraga-{n}"
        base, i = key, 2
        while key in seen:
            suffix = f"-{i}"
            key = base[: 40 - len(suffix)] + suffix
            i += 1
        seen.add(key)
        item["key"] = key


def clean_fields(block_type, fields, *, where="", media_ids=None):
    """Fälten rensade mot typens schema: okända fält och för lång text är
    fel (BlockError), HTML tas bort och AI-typografi normaliseras."""
    errors = []
    where = where or block_type.name
    if not isinstance(fields, dict):
        raise BlockError(f"{where}: fälten ska vara ett dict.")
    unknown = set(fields) - {f.key for f in block_type.fields}
    if unknown:
        errors.append(f"{where}: okända fält {', '.join(sorted(unknown))}.")
    cleaned = {}
    for spec in block_type.fields:
        cleaned[spec.key] = _clean_value(
            spec, fields.get(spec.key), f"{where}, {spec.label}", errors, media_ids
        )
    if errors:
        raise BlockError(errors)
    return cleaned


def _clean_version(version, vwhere, block_type, media_ids, seen_versions, errors):
    """En version rensad mot schemat, eller None med felen i errors."""
    if not isinstance(version, dict):
        errors.append(f"{vwhere}: ska vara ett dict.")
        return None
    unknown = set(version) - VERSION_KEYS
    if unknown:
        errors.append(f"{vwhere}: okända nycklar {', '.join(sorted(unknown))}.")
    version_id = version.get("id")
    if not isinstance(version_id, str) or not VERSION_ID.fullmatch(version_id):
        errors.append(f"{vwhere}: id ska vara v_ och tolv bokstäver eller siffror.")
    elif version_id in seen_versions:
        errors.append(f"{vwhere}: samma id som en annan version.")
    if version.get("source") not in SOURCES:
        errors.append(f"{vwhere}: okänd källa {version.get('source')!r}.")
    by = version.get("by")
    if by is not None and (isinstance(by, bool) or not isinstance(by, int) or by < 1):
        errors.append(f"{vwhere}: by ska vara en användares id eller null.")
    if not _valid_iso(version.get("at")):
        errors.append(f"{vwhere}: at ska vara en tid i ISO 8601.")
    try:
        fields = clean_fields(
            block_type, version.get("fields") or {}, where=vwhere, media_ids=media_ids
        )
    except BlockError as exc:
        errors.extend(exc.errors)
        return None
    if errors:
        return None
    clean = {
        "id": version_id,
        "fields": fields,
        "source": version.get("source"),
        "by": by,
        "at": version.get("at"),
    }
    sig = version.get("sig")
    if isinstance(sig, str) and SIGNATURE.fullmatch(sig):
        clean["sig"] = sig
    return clean


def _valid_iso(value):
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_blocks(blocks, *, account=None):
    """Schemakontrollen för en sidas block (utkastet som sparas). Returnerar
    en rensad kopia: HTML borttagen, typografin normaliserad, frågornas
    nycklar ifyllda. Kastar BlockError med alla fel: okänd typ, variant
    eller fält, för lång text, fel form på id:n eller versioner, och (med
    account) bilder som inte finns i kontots mediaarkiv.

    Bara den aktiva versionen kan stoppa sparningen. En äldre version som
    inte klarar schemat (till exempel ett telefonnummer från innan
    gränserna fanns, eller en bild som tagits bort) tas bort ur kopian i
    stället, så att en gammal version aldrig gör sidan omöjlig att spara.
    En signatur som inte har rätt form tas bort (is_signed prövar den)."""
    if not isinstance(blocks, list):
        raise BlockError("Blocken ska vara en lista.")
    errors = []
    if len(blocks) > MAX_BLOCKS:
        errors.append(f"Högst {MAX_BLOCKS} block på en sida (nu {len(blocks)}).")
    media_ids = None
    if account is not None:
        media_ids = set(MediaAsset.objects.filter(account=account).values_list("pk", flat=True))
    out, seen_blocks = [], set()
    for n, raw in enumerate(blocks, start=1):
        where = f"Block {n}"
        if not isinstance(raw, dict):
            errors.append(f"{where}: ska vara ett dict.")
            continue
        unknown = set(raw) - BLOCK_KEYS
        if unknown:
            errors.append(f"{where}: okända nycklar {', '.join(sorted(unknown))}.")
        block_type = TYPES.get(raw.get("type"))
        if block_type is None:
            errors.append(f"{where}: okänd blocktyp {raw.get('type')!r}.")
            continue
        where = f"Block {n} ({block_type.name})"
        block_id = raw.get("id")
        if not isinstance(block_id, str) or not BLOCK_ID.fullmatch(block_id):
            errors.append(f"{where}: id ska vara b_ och tolv bokstäver eller siffror.")
        elif block_id in seen_blocks:
            errors.append(f"{where}: samma id som ett annat block.")
        seen_blocks.add(block_id)
        variant = raw.get("variant")
        if block_type.variant(variant) is None:
            errors.append(f"{where}: okänd variant {variant!r}.")
        versions = raw.get("versions")
        if not isinstance(versions, list) or not versions:
            errors.append(f"{where}: minst en version behövs.")
            continue
        if len(versions) > MAX_VERSIONS:
            errors.append(f"{where}: högst {MAX_VERSIONS} versioner (nu {len(versions)}).")
        clean_versions, seen_versions = [], set()
        active_id = raw.get("active")
        for m, version in enumerate(versions, start=1):
            vwhere = f"{where}, version {m}"
            verrors = []
            clean = _clean_version(version, vwhere, block_type, media_ids, seen_versions, verrors)
            is_active = isinstance(version, dict) and version.get("id") == active_id
            if verrors:
                if is_active:
                    errors.extend(verrors)
                else:
                    # En äldre version som inte klarar schemat följer inte med.
                    logger.info("Sidbyggaren: en gammal version togs bort: %s", verrors[0])
                continue
            seen_versions.add(clean["id"])
            clean_versions.append(clean)
        if active_id not in seen_versions and not any(
            isinstance(v, dict) and v.get("id") == active_id for v in versions
        ):
            errors.append(f"{where}: den aktiva versionen finns inte bland versionerna.")
        out.append(
            {
                "id": block_id,
                "type": block_type.key,
                "variant": variant,
                "active": raw.get("active"),
                "versions": clean_versions,
            }
        )
    if errors:
        raise BlockError(errors)
    return out


# ---------------------------------------------------------------------------
# Formuläret
# ---------------------------------------------------------------------------

#: Högst så många frågor ritas (public_views.QUESTIONS_MAX förut).
QUESTIONS_MAX = 8


@dataclass
class FormSpec:
    """Formuläret på sidan, ur det första formulärblocket (eller en
    standard när sidan saknar formulär och någon ändå postar).

    short: mobil och namn (valfritt), som "ringer direkt" förut.
    questions: frågorna, ett meddelande (inte när en fråga redan är en
    längre text), namn (krävs), telefon och e-post, som "offert" förut.
    booking: frågorna (dag och tid), ett meddelande, namn och telefon;
    tiden bekräftas i telefon, så ingen e-post.

    fields räknar fälten som besökaren ser (Konverteringskollen)."""

    variant: str = "short"
    title: str = ""
    questions: list = field(default_factory=list)
    note_title: str = ""
    note: str = ""
    submit: str = ""
    block_id: str = ""

    @property
    def is_short(self):
        return self.variant == "short"

    @property
    def name_required(self):
        return not self.is_short

    @property
    def asks_message(self):
        if self.is_short:
            return False
        return not any(q.get("kind") == "textarea" for q in self.questions)

    @property
    def asks_email(self):
        return self.variant == "questions"

    @property
    def fields(self):
        """Fälten i formuläret: frågorna, namn och telefon, meddelandet och
        e-posten när de visas."""
        return len(self.questions) + 2 + int(self.asks_message) + int(self.asks_email)


def _questions(raw_questions):
    questions, seen = [], set()
    for raw in raw_questions or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        label = str(raw.get("label") or "").strip()
        kind = raw.get("kind") if raw.get("kind") in PAGE_QUESTION_KINDS else "text"
        if not key or not label or key in seen:
            continue
        seen.add(key)
        questions.append({"key": key, "label": label[:120], "kind": kind, "field": f"q_{key}"})
        if len(questions) >= QUESTIONS_MAX:
            break
    return questions


def form_spec(blocks):
    """FormSpec för det första formulärblocket, eller None."""
    for block in blocks:
        if block.get("type") != "form":
            continue
        fields = active_fields(block)
        variant = block.get("variant") if block.get("variant") in registry.FORM_TITLES else "short"
        return FormSpec(
            variant=variant,
            title=fields.get("title") or registry.FORM_TITLES[variant],
            questions=_questions(fields.get("questions")),
            note_title=fields.get("note_title") or "",
            note=fields.get("note") or "",
            submit=fields.get("submit") or registry.FORM_SUBMITS[variant],
            block_id=block.get("id") or "",
        )
    return None


# ---------------------------------------------------------------------------
# Från förslagets innehåll (generatorn)
# ---------------------------------------------------------------------------


def blocks_from_content(content, mode, account, *, user=None, ctx=None, now=None):
    """Block ur förslagets sidinnehåll (generator.build_page, samma form som
    det gamla Campaign.page): rubriken (tjänsten och orten) blir Toppens
    överrubrik och rubriken säger vad kunden får (registry.HERO_TITLES),
    punkterna och telefonen följer med, formulärets rubrik, frågor och text
    blir Formulär, och sättet att sälja väljer varianterna:

        ringer direkt   Toppen med ringknapp (med bild när kontot har en),
                        kort formulär och ringremsa
        offert          Toppen med formulär och formulär med frågor
        boka tid        Toppen med formulär och formulär för att boka tid

    Ringremsan kommer bara med när sidan har ett nummer. Frågornas nycklar
    och etiketter följer med oförändrade (Lead.answers använder etiketten).
    Migreringen 0011 har en egen, fryst kopia av samma mappning."""
    from .. import generator
    from .facts import tidy_points

    content = content if isinstance(content, dict) else {}
    # Ett nummer ur texten (generator.one_phone), och texterna inom
    # schemats gränser: förslaget fäller aldrig sidan (en lång tjänst och
    # ort, en telefonuppgift med två nummer).
    phone = generator.one_phone(content.get("phone"))
    form_variant = registry.FORM_VARIANT_BY_MODE.get(mode, "questions")
    hero_variant = "call" if mode == Service.SALES_CALL else "form"
    note = str(content.get("note") or "").strip()[: registry.NOTE_MAX]
    points = [p[: registry.LINE_MAX] for p in tidy_points(content.get("points") or [])]
    # Tjänsten och orten (förslagets rubrik) står i överrubriken, och
    # rubriken säger vad kunden får (registry.HERO_TITLES).
    title = str(content.get("title") or "").strip()[: registry.TITLE_MAX]
    lead = str(content.get("lead") or "").strip()[: registry.LEAD_MAX]
    kicker = registry.hero_kicker(ctx.service, ctx.places) if ctx is not None else ""
    benefit = registry.hero_title(mode)
    if benefit and (kicker or title):
        kicker = kicker or title[: registry.KICKER_MAX]
        title = benefit
        company = registry.facts_for(account).company if account is not None else ""
        lead = registry.HERO_LEADS.get(mode, lead).format(c=company or "Vi")
    hero = {
        "kicker": kicker if kicker != title else "",
        "title": title,
        "lead": lead,
        "points": points[:4],
        "phone": phone,
        "image": None,
    }
    if mode == Service.SALES_CALL and phone and account is not None:
        # Bilden först när kontot har bilder: den senaste (inte logotypen),
        # med ringknappen kvar som huvudhandling. Kunden byter den i arkivet.
        latest = (
            MediaAsset.objects.filter(account=account, is_logo=False)
            .order_by("-pk")
            .values_list("pk", flat=True)
            .first()
        )
        if latest is not None:
            hero_variant, hero["image"] = "image", latest
    questions = []
    for q in content.get("questions") or []:
        if not isinstance(q, dict):
            continue
        key = str(q.get("key") or "")
        questions.append(
            {
                "key": key if QUESTION_KEY.fullmatch(key) else "",
                "label": str(q.get("label") or "")[: registry.QUESTION_MAX],
                "kind": q.get("kind") if q.get("kind") in PAGE_QUESTION_KINDS else "text",
            }
        )
    form = {
        "title": str(content.get("form_title") or "").strip()[: registry.TITLE_MAX]
        or registry.FORM_TITLES[form_variant],
        "questions": questions[:8],
        # Rutan vid formuläret: rubriken upprepar inte texten ("medan du väntar").
        "note_title": "Bra att veta" if mode == Service.SALES_CALL and note else "",
        "note": note,
        "submit": registry.FORM_SUBMITS[form_variant],
    }
    out = [
        _built("hero", hero_variant, hero, user, now),
        _built("form", form_variant, form, user, now),
    ]
    if mode == Service.SALES_CALL and phone:
        company = registry.facts_for(account).company if account is not None else ""
        title = f"Ring {company}".strip()[: registry.SHORT_MAX] if company else "Ring oss"
        out.append(_built("callbar", "call", {"title": title, "phone": phone}, user, now))
    return out


def _built(type_key, variant, fields, user, now):
    block_type = TYPES[type_key]
    version = _version(clean_fields(block_type, fields), SOURCE_TEMPLATE, user, now)
    return {
        "id": new_block_id(),
        "type": type_key,
        "variant": variant,
        "active": version["id"],
        "versions": [version],
    }


def build_ctx(campaign):
    """BuildContext för en kampanj: tjänsten, orterna, sättet att sälja och
    tjänstens pris (generator.page_price: aldrig en annan tjänsts pris)."""
    return service_ctx(
        campaign.account,
        campaign.service if campaign.service_id else None,
        places=_places(campaign.area),
    )


def _places(area):
    from .. import generator

    return generator.places_of(area)


def service_ctx(account, service, *, places=None):
    """BuildContext för en sida om tjänsten (service kan vara None): orterna
    (places, annars ur uppgiften om område), sättet att sälja och tjänstens
    pris."""
    from .. import generator
    from .facts import facts_for

    if places is None:
        places = list(facts_for(account).places)
    name = service.name if service is not None else ""
    price, label = generator.page_price(account, name)
    return BuildContext(
        service=name,
        places=list(places),
        mode=service.sales_mode if service is not None else "",
        price=price,
        price_label=label,
    )


def block_rows(blocks):
    """Blocken som rader för en lista (fliken Sidan, granskningen):
    typens namn och ikon, variantens namn och blockets rubrik (title, eller
    name för Personen bakom)."""
    rows = []
    for block in blocks:
        block_type = TYPES.get(block.get("type"))
        if block_type is None:
            continue
        variant = block_type.variant(block.get("variant"))
        fields = active_fields(block)
        rows.append(
            {
                "id": block.get("id", ""),
                "name": block_type.name,
                "icon": block_type.icon,
                "variant": variant.name if variant else "",
                "headline": fields.get("title") or fields.get("name") or "",
            }
        )
    return rows


def copy_blocks(blocks):
    """En djup kopia (block delas aldrig mellan sidor som samma objekt)."""
    return copy.deepcopy(list(blocks))
