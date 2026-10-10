"""
Skriv med AI i utskick (README F.7): sms-texten och fälten i ett Brev-block.

    WRITE_SMS_TOOL       en GSM-7-text, högst en del med platshållarna och
                         avregistreringsraden, inga länkar utom {länk:x}
    WRITE_BLOCK_TOOL     fälten i ett Brev-block (email.registry.EMAIL_TYPES)
    make_utskick_guard(account, utskick) -> Guard
    write_sms(utskick, *, user, brief="") -> AiResult(text)
    write_block(utskick, block_type, *, user, brief="", block=None) -> AiResult(fields)
    @dataclass AiResult(ok, fields, text, warnings, error, source, note)

Allt återanvänder sidbyggarens AI (apps.flamingo.pagebuilder.ai):
ask_model (aldrig för demokontot, bara när AI är inkopplad, inom
dygnsbudgeten och kontots dagsgräns limits.reserve_ai, som delas med
sidbyggaren, med en tidsgräns), HARD_RULES och Guard. När AI inte svarar
blir förslaget mallens: blockets mallfält (email.blocks.new_block), eller en
av sms-mallarna (composer.TEMPLATES). Inget sparas här: redigeraren visar
förslaget och sparar det när kunden använder det.

Vakten (make_utskick_guard) byggs som pagebuilder.ai.make_base men ur
utskicket: kontots bekräftade uppgifter plus villkoren i erbjudandet
(Utskick.confirmed_terms), som går in bland uppgifterna (fact_values) och
inte bland extra: bara uppgifterna släpper igenom brådska och löften om tid
(checks.build_context, fact_text). Villkoren räknas först när kunden bockat
"Uppgifterna stämmer" (terms_confirmed_at). Så går "Erbjudandet gäller till
31 oktober", "VARME26" och "kl. 15" igenom bara med bekräftade villkor.
Beloppen som får stå är bekräftade priser och beloppen i villkoren.

AI:s text som inte klarar vakten används inte; mallens eller den nuvarande
texten står kvar, och anmärkningen följer med som varning. Kundens egen text
stoppas aldrig av vakten (F.5: den får varningar i kontrollerna). AI skriver
aldrig priser, erbjudanden eller koder i information (H.5): sådan text kastas
(sending.checks.looks_like_ad), och blocken för erbjudanden och priser är
låsta för information.
"""

import logging
import re
from dataclasses import dataclass, field

from apps.common.security import sanitize_multiline_text, sanitize_plain_text
from apps.flamingo import checks as fact_checks
from apps.flamingo import generator
from apps.flamingo.pagebuilder import ai as page_ai

from . import composer
from .models import INFORMATION, TrackedLink

logger = logging.getLogger(__name__)

SMS_TIMEOUT = 15.0
BLOCK_TIMEOUT = 20.0
SMS_MAX_TOKENS = 600
BLOCK_MAX_TOKENS = 1500
#: Största önskemål kunden kan skriva till AI.
BRIEF_MAX = 300

#: Fältsorterna AI skriver i ett block. Länkar, datum, tider, koder,
#: telefon, e-post, bilder och val skriver kunden själv.
TEXT_KINDS = ("text", "textarea", "rich_basic")
#: Fält AI aldrig skriver: namn, priserna och öppettiderna kommer ur kundens
#: uppgifter eller skrivs av kunden ("typ" för hela blocket, "typ.fält").
NOT_WRITTEN = frozenset(
    {
        "prices.items",
        "person.name",
        "person.role",
        "signature.name",
        "signature.script_name",
        "signature.line",
        "hours.map_text",
        "social.items",
        "gallery.items",
    }
)

NOTE_INFORMATION = "Erbjudanden hör inte hemma i information."
NOTE_NOTHING = "Det här blocket har ingen text som AI kan skriva."
NOTE_UNKNOWN = "Blocket finns inte i Brev."
NOTE_SMS_TEMPLATE = (
    "AI svarade inte, så förslaget är en av mallarna. Ändra det så att det låter som du."
)

WRITE_SMS_TOOL = {
    "name": "skriv_sms",
    "description": "Lämna texten till sms-utskicket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Hela sms-texten, med platshållarna som klamrar.",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}

WRITE_BLOCK_TOOL = {
    "name": "skriv_blocket",
    "description": "Lämna texterna till ett block i mejlet.",
    "input_schema": {
        "type": "object",
        "properties": {
            "falt": {
                "type": "object",
                "description": (
                    "Fältens nycklar med sina texter. En lista lämnas som en lista med "
                    "objekt med underfältens nycklar."
                ),
            }
        },
        "required": ["falt"],
        "additionalProperties": False,
    },
}

SMS_SYSTEM = f"""\
Du skriver ett sms-utskick från ett svenskt lokalt företag till företagets \
egna kunder. Svara bara genom att anropa verktyget {WRITE_SMS_TOOL["name"]}.

UPPDRAG
- Skriv en kort, vänlig text i du-form med enkla ord, högst "max_tecken" \
tecken räknat med platshållarna. Inga utropstecken och inga emojier.
- Använd bara tecknen i sms-alfabetet GSM-7: vanliga bokstäver, å, ä, ö, \
siffror och vanliga skiljetecken.
- Börja gärna med "Hej {{förnamn|du}},". Platshållarna är {{förnamn}}, \
{{efternamn}}, {{namn}} och {{företag}}, med en reservtext efter ett streck.
- En länk skrivs bara som en av länkarna under "länkar", som {{länk:nyckel}}. \
Skriv aldrig en adress.
- Skriv inte om avregistrering: den raden läggs till automatiskt.
- Står "företagsnamn_krävs" i indata: skriv företagets namn i texten.
- Är syftet information: skriv bara informationen, inga erbjudanden, \
priser, rabatter eller koder.

{page_ai.HARD_RULES}
"""

BLOCK_SYSTEM = f"""\
Du skriver texterna till ett block i ett mejl från ett svenskt lokalt \
företag till företagets egna kunder. Mejlet är enkelt och personligt, som \
ett vanligt brev. Svara bara genom att anropa verktyget \
{WRITE_BLOCK_TOOL["name"]}.

UPPDRAG
- Skriv bara fälten under "skriv", med samma nycklar, och håll dig under \
"max_tecken" för varje fält. En lista har högst "max_antal" poster.
- Enkel, konkret svenska i du-form med korta meningar. Inga utropstecken \
och inga emojier.
- Ett fält av sorten rich_basic får ha stycken (en tom rad emellan), \
**fet** text, *kursiv* text och rader som börjar med "- " som en lista. \
Inga länkar och inga adresser.
- Platshållarna {{förnamn}}, {{efternamn}}, {{namn}} och {{företag}} får \
användas, med en reservtext efter ett streck: {{förnamn|du}}.
- "nu" visar vad som står nu. "önskemål" är kundens önskan om innehållet.
- Är syftet information: skriv bara informationen, inga erbjudanden, \
priser, rabatter eller koder.

{page_ai.HARD_RULES}
"""


@dataclass
class AiResult:
    ok: bool
    #: Blockets fält som AI (eller mallen) skrev: {nyckel: värde}.
    fields: dict = field(default_factory=dict)
    #: Sms-texten.
    text: str = ""
    #: Anmärkningar från vakten på det som inte användes.
    warnings: list = field(default_factory=list)
    #: Varför inget förslag finns (ok är False).
    error: str = ""
    #: "ai" eller "mallar".
    source: str = ""
    #: Varför förslaget kommer ur mallarna (AI av, demo, budget, tid).
    note: str = ""

    def as_json(self):
        return {
            "ok": self.ok,
            "fields": self.fields,
            "text": self.text,
            "warnings": self.warnings,
            "error": self.error,
            "source": self.source,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Vakten
# ---------------------------------------------------------------------------


def confirmed_terms(utskick):
    """Villkoren i erbjudandet som text ("Erbjudandet gäller till 31
    oktober"), bara när kunden bockat "Uppgifterna stämmer"."""
    if not utskick.terms_confirmed_at:
        return []
    out = []
    for term in utskick.confirmed_terms or []:
        if not isinstance(term, dict):
            continue
        text = " ".join(f"{term.get('label') or ''} {term.get('value') or ''}".split())
        if text:
            out.append(text)
    return out


def _social(account):
    from apps.flamingo.pagebuilder.render import _rating_fact

    try:
        return bool(
            account.selected_google_reviews()
            or account.trusted_google_rating is not None
            or _rating_fact(account)
        )
    except Exception:  # noqa: BLE001 - omdömen är ett tillägg; utan dem gäller den strängare regeln
        logger.info("Utskick: omdömena för konto %s gick inte att läsa", account.pk)
        return False


def make_utskick_guard(account, utskick):
    """Vakten för AI:s texter i ett utskick (F.7), som pagebuilder.ai.Guard."""
    rows = list(account.usable_fact_rows())
    customer = getattr(account, "customer", None)
    company = generator.company_name(customer) if customer is not None else ""
    terms = confirmed_terms(utskick)
    context = fact_checks.build_context(
        list(account.confirmed_facts().values()) + terms,
        extra=[getattr(customer, "name", ""), company],
    )
    amounts = {
        generator.price_amount(fact.value)
        for fact in rows
        if generator.fact_kind(fact) == "price" or fact.key.startswith(generator.PRICE_PREFIX)
    }
    for term in terms:
        amounts |= page_ai._amounts(term)
    amounts -= {""}
    claims_text = " ".join(f"{fact.label} {fact.value}" for fact in rows).lower()
    display = _display_name(account)
    grounding = " ".join([context.fact_text, company.lower(), display.lower(), claims_text])
    return page_ai.Guard(
        context=context,
        grounding=grounding,
        claims_text=claims_text,
        amounts=frozenset(amounts),
        social=_social(account),
    )


def _display_name(account):
    from .access import settings_for

    row = settings_for(account)
    return (row.display_name or "").strip() or (
        account.customer.name if getattr(account, "customer_id", None) else ""
    )


def _facts(account):
    out = []
    for fact in account.usable_fact_rows():
        value = generator._clean(fact.value)
        if value:
            out.append({"uppgift": generator._clean(fact.label, 120), "värde": value})
    return out


def _purpose(utskick):
    if utskick.purpose == INFORMATION:
        reason = utskick.get_info_reason_display() if utskick.info_reason else ""
        if utskick.info_reason == "annat" and utskick.info_reason_text:
            reason = utskick.info_reason_text
        return f"information: {reason.lower()}" if reason else "information"
    return "reklam"


def _brief(brief):
    return sanitize_plain_text(str(brief or ""), max_length=BRIEF_MAX)[:BRIEF_MAX]


def _problems(guard, utskick, text):
    """Vaktens anmärkningar på en AI-text, plus reglerna för information
    och platshållarna (en okänd platshållare används aldrig)."""
    from .sending import checks

    found = list(guard.problems(text))
    if utskick.purpose == INFORMATION and checks.looks_like_ad(text):
        found.append(NOTE_INFORMATION)
    placeholders = composer.placeholders(text)
    if placeholders.unknown or placeholders.links or placeholders.unsubscribe:
        found.append("Bara platshållarna för namn och företag.")
    if "!" in text:
        found.append("Inga utropstecken.")
    return found


# ---------------------------------------------------------------------------
# Sms
# ---------------------------------------------------------------------------

_LINK_TOKEN = re.compile(r"\{länk:[^{}]*\}")


def _template_sms(utskick, display_name):
    """Mallens text när AI inte svarar: information får mallen för
    öppettider, reklam påminnelsen. En länk som utskicket inte har tas bort
    (eller blir utskickets första länk)."""
    key = "oppettider" if utskick.purpose == INFORMATION else "paminnelse"
    text = composer.template_body(key, display_name)
    first = TrackedLink.objects.filter(utskick=utskick).order_by("pk").first()
    if first is not None:
        return _LINK_TOKEN.sub("{" + composer.LINK_PREFIX + first.key + "}", text)
    text = re.sub(r"[:\s]*\{länk:[^{}]*\}", "", text)
    return " ".join(text.split())


def _sms_room(utskick):
    """Tecken kvar för texten i en GSM-7-del när avregistreringsraden läggs
    till sist (svarsnumret eller länken)."""
    line = composer.opt_out_line(utskick.sms_sender_kind)
    return max(60, 160 - len(line) - 2)


def write_sms(utskick, *, user, brief=""):
    """Ett förslag till sms-texten (F.7): en GSM-7-del med platshållarna
    och avregistreringsraden, inga länkar utom utskickets egna {länk:x}."""
    from .sending import checks

    account = utskick.account
    display = _display_name(account)
    links = [
        {"nyckel": link.key, "vad": link.label or link.key}
        for link in TrackedLink.objects.filter(utskick=utskick).order_by("pk")
    ]
    payload = {
        "företag": display,
        "syfte": _purpose(utskick),
        "max_tecken": _sms_room(utskick),
        "uppgifter": _facts(account),
        "villkor": confirmed_terms(utskick),
        "länkar": links,
        "nuvarande_text": utskick.sms_body or "",
        "önskemål": _brief(brief),
    }
    if composer.is_reply_sender(composer.sender_for_kind(utskick)):
        payload["företagsnamn_krävs"] = display
    data, note = page_ai.ask_model(
        account,
        system=SMS_SYSTEM,
        payload=payload,
        tool=WRITE_SMS_TOOL,
        user=user,
        timeout=SMS_TIMEOUT,
        max_tokens=SMS_MAX_TOKENS,
    )
    template = _template_sms(utskick, display)
    if data is None:
        return AiResult(ok=True, text=template, source=page_ai.SOURCE_TEMPLATES_LABEL, note=note)
    raw = data.get("text") if isinstance(data, dict) else None
    text = sanitize_multiline_text(str(raw or ""), max_length=2000).strip()
    warnings = []
    if not text:
        warnings.append("AI lämnade ingen text.")
    else:
        guard = make_utskick_guard(account, utskick)
        found = list(guard.problems(text))
        if utskick.purpose == INFORMATION and checks.looks_like_ad(text):
            found.append(NOTE_INFORMATION)
        found += composer.validate(account, text, utskick)
        if "!" in text:
            found.append("Inga utropstecken.")
        if not checks.sender_identified(utskick, text):
            found.append(f"Skriv {display} i texten.")
        if not found:
            shown = composer.preview(utskick, body=text, with_longest=False)
            if shown["parts"] > 1 or shown["non_gsm"]:
                found.append("Texten blev längre än ett sms.")
        warnings += found
    if warnings:
        return AiResult(
            ok=True,
            text=template,
            warnings=_unique(warnings),
            source=page_ai.SOURCE_TEMPLATES_LABEL,
            note=NOTE_SMS_TEMPLATE,
        )
    return AiResult(ok=True, text=text, source=page_ai.SOURCE_AI_LABEL)


# ---------------------------------------------------------------------------
# Ett block i Brev
# ---------------------------------------------------------------------------


def _unique(items):
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _spec_dict(spec):
    return spec.as_dict() if hasattr(spec, "as_dict") else dict(spec)


def writable(block_type, variant=""):
    """Fälten AI skriver i blocket: [(nyckel, fältets dict)] för text,
    textarea och rich_basic, och listor vars underfält är text (bara de
    underfälten). Namn, priser och länkar skrivs aldrig (NOT_WRITTEN)."""
    out = []
    for spec in getattr(block_type, "fields", ()):
        data = _spec_dict(spec)
        key = data.get("key", "")
        if f"{block_type.key}.{key}" in NOT_WRITTEN:
            continue
        if data.get("variants") and variant and variant not in data["variants"]:
            continue
        kind = data.get("kind")
        if kind in TEXT_KINDS:
            out.append((key, data))
        elif kind == "items":
            subs = [s for s in data.get("items") or [] if s.get("kind") in TEXT_KINDS]
            if subs:
                out.append((key, {**data, "items": subs}))
    return out


def _schema_for(fields):
    out = {}
    for key, data in fields:
        if data.get("kind") == "items":
            out[key] = {
                "vad": data.get("label", key),
                "max_antal": data.get("max_items") or 6,
                "poster": {
                    sub["key"]: {
                        "vad": sub.get("label", sub["key"]),
                        "max_tecken": sub.get("max_length") or 200,
                        "sort": sub.get("kind"),
                    }
                    for sub in data["items"]
                },
            }
        else:
            out[key] = {
                "vad": data.get("label", key),
                "max_tecken": data.get("max_length") or 200,
                "sort": data.get("kind"),
            }
    return out


_MD_LINK = re.compile(r"\[([^\]]{1,120})\]\([^)]*\)")


def _clean_text(raw, data, guard, utskick, where, warnings):
    """En text från AI, rensad och prövad, eller None (med en varning)."""
    if not isinstance(raw, str):
        return None
    kind = data.get("kind")
    limit = int(data.get("max_length") or 200)
    if kind == "text":
        text = sanitize_plain_text(raw, max_length=limit * 4).strip()
    else:
        text = sanitize_multiline_text(raw, max_length=limit * 4).strip()
    if kind == "rich_basic":
        text = _MD_LINK.sub(lambda m: m.group(1), text)
    if not text:
        return None
    if len(text) > limit:
        warnings.append(f"{where}: texten blev för lång.")
        return None
    found = _problems(guard, utskick, text)
    if found:
        warnings.append(f"{where}: {found[0]}")
        return None
    return text


def _current_fields(block):
    if not isinstance(block, dict):
        return {}
    if isinstance(block.get("versions"), list):
        from .email import blocks as email_blocks

        try:
            return dict(email_blocks.active_fields(block) or {})
        except Exception:  # noqa: BLE001 - ett trasigt block ger inga nuvarande fält
            return {}
    fields = block.get("fields")
    return dict(fields) if isinstance(fields, dict) else {}


def _payload_now(fields, current):
    out = {}
    for key, data in fields:
        value = current.get(key)
        if data.get("kind") == "items":
            rows = value if isinstance(value, list) else []
            subs = [s["key"] for s in data["items"]]
            out[key] = [
                {k: row.get(k, "") for k in subs} for row in rows[:12] if isinstance(row, dict)
            ]
        elif isinstance(value, str):
            out[key] = value
    return out


def _apply(raw_fields, fields, current, guard, utskick, warnings):
    """AI:s fält som används: {nyckel: värde}. En lista ersätts bara med
    giltiga poster; underfält som AI inte skriver står kvar från den
    nuvarande posten på samma plats."""
    used = {}
    if not isinstance(raw_fields, dict):
        return used
    for key, data in fields:
        label = data.get("label", key)
        raw = raw_fields.get(key)
        if data.get("kind") != "items":
            text = _clean_text(raw, data, guard, utskick, label, warnings)
            if text is not None:
                used[key] = text
            continue
        if not isinstance(raw, list) or not raw:
            continue
        before = current.get(key) if isinstance(current.get(key), list) else []
        max_items = int(data.get("max_items") or 6)
        rows = []
        for index, item in enumerate(raw[:max_items]):
            if not isinstance(item, dict):
                continue
            row = (
                dict(before[index])
                if index < len(before) and isinstance(before[index], dict)
                else {}
            )
            ok = False
            for sub in data["items"]:
                where = f"{data.get('item_label') or label} {index + 1}"
                text = _clean_text(item.get(sub["key"]), sub, guard, utskick, where, warnings)
                if text is not None:
                    row[sub["key"]] = text
                    ok = True
            if ok:
                rows.append(row)
        if rows:
            used[key] = rows
    return used


def _type_for(block_type):
    from .email import registry

    if isinstance(block_type, str):
        return registry.get_type(block_type)
    return block_type


def _template_fields(utskick, type_key, fields, user):
    """Mallens fält för blocket (email.blocks.new_block), bara de AI skriver."""
    from .email import blocks as email_blocks

    try:
        block = email_blocks.new_block(type_key, utskick.account, utskick, user=user)
    except Exception:  # noqa: BLE001 - ingen mall är inget förslag, aldrig ett fel
        logger.info("Utskick %s: mallen för %s gick inte att bygga", utskick.pk, type_key)
        return {}
    current = _current_fields(block)
    wanted = {key for key, _data in fields}
    return {key: value for key, value in current.items() if key in wanted and value}


def _no_template_text(note):
    """AI svarade inte och mallen har ingen text för blocket: varför, och
    att kunden skriver själv ("AI är inte inkopplad. Skriv texten ...")."""
    reason = (note or page_ai.NOTE_ERROR).split(", så förslaget")[0].rstrip(".")
    return f"{reason}. Mallen har ingen text för blocket, så skriv texten själv."


def write_block(utskick, block_type, *, user, brief="", block=None):
    """Ett förslag till fälten i ett Brev-block (F.7). block är blocket som
    det står i redigeraren (med versions) eller {"fields": {...}}; AI ser
    den nuvarande texten och skriver om den."""
    from .email import registry

    kind = _type_for(block_type)
    if kind is None:
        return AiResult(ok=False, error=NOTE_UNKNOWN)
    if utskick.purpose == INFORMATION and kind.key in registry.OFFER_KEYS:
        return AiResult(ok=False, error=NOTE_INFORMATION)
    variant = (block or {}).get("variant") if isinstance(block, dict) else ""
    fields = writable(kind, variant or "")
    if not fields:
        return AiResult(ok=False, error=NOTE_NOTHING)
    account = utskick.account
    current = _current_fields(block)
    payload = {
        "företag": _display_name(account),
        "syfte": _purpose(utskick),
        "ämnesrad": utskick.subject or "",
        "block": getattr(kind, "name", kind.key),
        "skriv": _schema_for(fields),
        "nu": _payload_now(fields, current),
        "uppgifter": _facts(account),
        "villkor": confirmed_terms(utskick),
        "önskemål": _brief(brief),
    }
    data, note = page_ai.ask_model(
        account,
        system=BLOCK_SYSTEM,
        payload=payload,
        tool=WRITE_BLOCK_TOOL,
        user=user,
        timeout=BLOCK_TIMEOUT,
        max_tokens=BLOCK_MAX_TOKENS,
    )
    if data is None:
        template = _template_fields(utskick, kind.key, fields, user)
        if not template:
            return AiResult(ok=False, error=_no_template_text(note), note=note)
        return AiResult(ok=True, fields=template, source=page_ai.SOURCE_TEMPLATES_LABEL, note=note)
    warnings = []
    guard = make_utskick_guard(account, utskick)
    used = _apply(data.get("falt"), fields, current, guard, utskick, warnings)
    if not used:
        template = _template_fields(utskick, kind.key, fields, user)
        return AiResult(
            ok=bool(template),
            fields=template,
            warnings=_unique(warnings),
            error="" if template else page_ai.NOTE_EMPTY,
            source=page_ai.SOURCE_TEMPLATES_LABEL,
            note=page_ai.NOTE_EMPTY,
        )
    return AiResult(
        ok=True, fields=used, warnings=_unique(warnings), source=page_ai.SOURCE_AI_LABEL
    )
