"""
Per-block-type edit schema + save/sanitize logic for the /manage/ block editor.

Each block type's `data` JSON has a known shape (see templates/website/blocks/*).
This module declares that shape so the editor can:
  * render the right inputs for one block type (build_form_context)
  * rebuild + sanitize block.data from a POST (clean_block_data)

Security: every value is run through the security boundary here - plain text
via sanitize_plain_text, rich via sanitize_rich_html_basic, URLs via
validate_url, images via validate_media_id. Unknown keys can't be injected
because we only read the keys declared in the schema.

Field types:
  plain   - single-line text (sanitised, no HTML)
  text    - multi-line plain text
  rich    - basic rich text (bold/italic/links + variables), Tiptap
  url     - validated URL
  image   - MediaFile id (rendered as an image picker)
  choice  - fixed set of options
  length  - CSS length (e.g. "4rem", "32px")

Dotted keys (e.g. "link.label") are stored nested in the JSON.
Lists declare repeating rows; `simple` lists are plain string arrays.
"""

import copy

from django.core.exceptions import ValidationError

from apps.common.security import (
    sanitize_multiline_text,
    sanitize_plain_text,
    sanitize_rich_html_basic,
    validate_media_id,
    validate_url,
)

ANCHOR_HELP = "Valfritt id för ankarlänk, t.ex. tjanster (ger #tjanster)."


def _f(key, type_, label, **extra):
    return {"key": key, "type": type_, "label": label, **extra}


_SIDE_CHOICES = [["left", "Bild till vänster"], ["right", "Bild till höger"]]

# ADX-designens komponentbibliotek (strict-design-guide.html). Varje posts
# fält speglar exakt vad blockmallen läser - mallvägen härleds ur typnamnet
# i _wrapper.html, så en ny typ = en post här + en mall, ingenting annat.
# "kicker"/"title"/"intro" är sektionshuvudet (.sec-head) där det finns.
BLOCK_EDIT_SCHEMA = {
    "hero": {
        "label": "Hero",
        "purpose": (
            "Sidhuvudet: stor rubrik (H1) med ingress och upp till två knappar, "
            "valfri bild under. ALLTID sidans första block - varje sida i "
            "designen börjar med ett hero."
        ),
        "fields": [
            _f(
                "kicker",
                "plain",
                "Kicker (liten rad ovanför)",
                help="T.ex. Digitalbyrå - Stockholm. Hakparenteserna ritas av designen.",
            ),
            _f("title", "text", "Rubrik", help="Radbrytningar behålls."),
            _f("lead", "text", "Ingress"),
            _f("primary.label", "plain", "Primär knapp - text"),
            _f("primary.url", "link", "Primär knapp - länk"),
            _f("secondary.label", "plain", "Sekundär knapp - text"),
            _f("secondary.url", "link", "Sekundär knapp - länk"),
            _f("image_id", "image", "Hero-bild (valfri)"),
        ],
        "lists": [],
    },
    "chips": {
        "label": "Nyckeltal (chips)",
        "purpose": (
            "Rad med nyckeltal i små pillerformade rutor, t.ex. '99,9 %' + "
            "'Upptid'. Ligger oftast direkt under hero som förtroendemarkör. "
            "Allt innehåll ligger i listan 'chips'."
        ),
        "fields": [],
        "lists": [
            {
                "key": "chips",
                "label": "Nyckeltal",
                "singular": "nyckeltal",
                "fields": [
                    _f("value", "plain", "Värde (t.ex. 99,9 %)"),
                    _f("label", "plain", "Etikett"),
                ],
            },
        ],
    },
    "marquee": {
        "label": "Rullande band",
        "purpose": (
            "Ett band med ord som rullar horisontellt i loop. Rent dekorativt, "
            "bryter av mellan sektioner. Allt innehåll ligger i listan 'items'."
        ),
        "fields": [],
        "lists": [
            {
                "key": "items",
                "label": "Ord",
                "singular": "ord",
                "simple": True,
                "fields": [
                    _f("text", "plain", "Text"),
                ],
            },
        ],
    },
    "svc_list": {
        "label": "Tjänstelista",
        "purpose": (
            "Listar sajtens AKTIVA tjänster automatiskt ur tjänstekatalogen. "
            "Fälten här är bara sektionshuvudet - själva korten kommer från "
            "tjänsterna och redigeras med tjänstverktygen."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("intro", "text", "Introtext"),
        ],
        "lists": [],
    },
    "case": {
        "label": "Case",
        "purpose": (
            "Ett kundcase: bild, rubrik, brödtext, valfri länk och en rad "
            "nyckeltal (listan 'stats')."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Sektionsrubrik"),
            _f("image_id", "image", "Bild"),
            _f("case_title", "plain", "Casets rubrik"),
            _f("body", "text", "Text"),
            _f("link.label", "plain", "Länktext (valfri)"),
            _f("link.url", "link", "Länkadress (valfri)"),
        ],
        "lists": [
            {
                "key": "stats",
                "label": "Siffror",
                "singular": "siffra",
                "fields": [
                    _f("value", "plain", "Värde"),
                    _f("label", "plain", "Etikett"),
                ],
            },
        ],
    },
    "steps": {
        "label": "Steg",
        "purpose": (
            "Numrerad arbetsgång, t.ex. 'Så går det till'. Varje steg har "
            "rubrik och text i listan 'steps'."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("intro", "text", "Introtext"),
        ],
        "lists": [
            {
                "key": "steps",
                "label": "Steg",
                "singular": "steg",
                "fields": [
                    _f("title", "plain", "Rubrik"),
                    _f("text", "text", "Text"),
                ],
            },
        ],
    },
    "quotes": {
        "label": "Citat",
        "purpose": (
            "Kundcitat med namn eller roll under. Två till tre citat räcker; de "
            "ligger i listan 'quotes'."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
        ],
        "lists": [
            {
                "key": "quotes",
                "label": "Citat",
                "singular": "citat",
                "fields": [
                    _f("text", "text", "Citat"),
                    _f("who", "plain", "Vem"),
                ],
            },
        ],
    },
    "why": {
        "label": "Varför vi",
        "purpose": (
            "Punkter om varför man ska välja er - rubrik plus text per punkt i "
            "ett rutnät (listan 'items')."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
        ],
        "lists": [
            {
                "key": "items",
                "label": "Punkter",
                "singular": "punkt",
                "fields": [
                    _f("title", "plain", "Rubrik"),
                    _f("text", "text", "Text"),
                ],
            },
        ],
    },
    "bar": {
        "label": "CTA-rad",
        "purpose": (
            "Smal avslutande uppmaningsrad (CTA): text till vänster, länkknapp "
            "till höger. Ligger SIST på nästan varje sida."
        ),
        "fields": [
            _f("label", "plain", "Text (vänster)"),
            _f("link.label", "plain", "Länktext"),
            _f("link.url", "link", "Länkadress"),
        ],
        "lists": [],
    },
    "split": {
        "label": "Bild + text",
        "purpose": (
            "Bild bredvid text i två spalter, med valfria punkter under texten "
            "(listan 'bullets'). Fältet image_side väljer vilken sida bilden "
            "står på."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("body", "text", "Text"),
            _f("image_id", "image", "Bild"),
            _f("image_side", "choice", "Bildens sida", choices=_SIDE_CHOICES),
        ],
        "lists": [
            {
                "key": "bullets",
                "label": "Punkter",
                "singular": "punkt",
                "simple": True,
                "fields": [
                    _f("text", "plain", "Punkt"),
                ],
            },
        ],
    },
    "folio": {
        "label": "Portfolio-kort",
        "purpose": (
            "Rutnät av portfoliokort med bild, titel och en kort metarad. "
            "Korten ligger i listan 'cards' och kan länkas."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("intro", "text", "Introtext"),
        ],
        "lists": [
            {
                "key": "cards",
                "label": "Kort",
                "singular": "kort",
                "fields": [
                    _f("image_id", "image", "Bild"),
                    _f("title", "plain", "Rubrik"),
                    _f("meta", "plain", "Metarad (t.ex. Webbplats · 2026)"),
                    _f("url", "link", "Länk (valfri)"),
                ],
            },
        ],
    },
    "plans": {
        "label": "Paket",
        "purpose": (
            "Prispaket bredvid varandra: namn, pris, beskrivning och "
            "punktlista. Paketen ligger i listan 'plans'; 'features' skrivs som "
            "en punkt per rad."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("intro", "text", "Introtext"),
        ],
        "lists": [
            {
                "key": "plans",
                "label": "Paket",
                "singular": "paket",
                "fields": [
                    _f("mini", "plain", "Minirad (överst)"),
                    _f("title", "plain", "Namn"),
                    _f("desc", "text", "Beskrivning"),
                    _f("price", "plain", "Pris (t.ex. 1 490 kr/mån)"),
                    _f("price_note", "plain", "Prisnot (under priset)"),
                    _f("features", "text", "Punkter - en per rad"),
                    _f("cta_label", "plain", "Knapptext"),
                    _f("cta_url", "link", "Knapplänk"),
                ],
            },
        ],
    },
    "compare": {
        "label": "Jämförelsetabell",
        "purpose": (
            "Jämförelsetabell med tre kolumner - en rad per egenskap i listan "
            "'rows'. Använd '+' och '-' i cellerna för ja och nej."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("col1", "plain", "Kolumn 1"),
            _f("col2", "plain", "Kolumn 2"),
            _f("col3", "plain", "Kolumn 3"),
        ],
        "lists": [
            {
                "key": "rows",
                "label": "Rader",
                "singular": "rad",
                "fields": [
                    _f("label", "plain", "Radetikett"),
                    _f("v1", "plain", "Värde 1 (+ / - / text)"),
                    _f("v2", "plain", "Värde 2"),
                    _f("v3", "plain", "Värde 3"),
                ],
            },
        ],
    },
    "team": {
        "label": "Team",
        "purpose": (
            "Personer med bild, namn och roll i ett rutnät (listan 'members'). "
            "Bilder kan du inte sätta, så raderna får namn och roll."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
        ],
        "lists": [
            {
                "key": "members",
                "label": "Personer",
                "singular": "person",
                "fields": [
                    _f("image_id", "image", "Foto"),
                    _f("name", "plain", "Namn"),
                    _f("role", "plain", "Roll"),
                ],
            },
        ],
    },
    "faq": {
        "label": "FAQ",
        "purpose": (
            "Visar en FAQ-sektions frågor som utfällbara rader. Sektionen väljs "
            "med faq_section (sektionens slug) - frågorna hämtas därifrån och "
            "skrivs inte i blocket."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("faq_section_id", "faq_section", "FAQ-sektion"),
        ],
        "lists": [],
    },
    "prose": {
        "label": "SEO-text",
        "purpose": (
            "Löpande brödtext med rubrik - SEO-texten. Enda blocket som tar "
            "formaterad text (fetstil, kursiv, länkar). Sidor har ofta två i "
            "rad."
        ),
        "fields": [
            _f("title", "plain", "Rubrik"),
            _f("body", "rich", "Text"),
        ],
        "lists": [],
    },
    "related": {
        "label": "Relaterade länkar",
        "purpose": "Rad med länkar till relaterade sidor (listan 'links').",
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
        ],
        "lists": [
            {
                "key": "links",
                "label": "Länkar",
                "singular": "länk",
                "fields": [
                    _f("label", "plain", "Text"),
                    _f("url", "link", "Länk"),
                ],
            },
        ],
    },
    "contact_cards": {
        "label": "Kontaktkort",
        "purpose": (
            "Kontaktuppgifter som kort - en rad per uppgift i listan 'cards', "
            "t.ex. kicker 'Mejl' och värde 'info@exempel.se', valfritt länkad."
        ),
        "fields": [],
        "lists": [
            {
                "key": "cards",
                "label": "Kort",
                "singular": "kort",
                "fields": [
                    _f("kicker", "plain", "Kicker (t.ex. E-post)"),
                    _f("value", "plain", "Värde (t.ex. hej@adx.se)"),
                    _f("url", "link", "Länk (valfri)"),
                ],
            },
        ],
    },
    "inquiry_form": {
        "label": "Förfrågningsformulär",
        "purpose": (
            "Renderar sajtens förfrågningsformulär. Fälten är bara "
            "sektionshuvudet - formuläret självt är inbyggt."
        ),
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("intro", "text", "Introtext"),
        ],
        "lists": [],
    },
    "newsletter": {
        "label": "Nyhetsbrev",
        "purpose": "Anmälan till nyhetsbrevet: rubrik, e-postfält och en not under.",
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("note", "plain", "Not (under fältet)"),
        ],
        "lists": [],
    },
    "spacer": {
        "label": "Mellanrum",
        "purpose": ("Tomt vertikalt utrymme. Använd bara när två sektioner behöver luft emellan."),
        "fields": [
            _f("height", "length", "Höjd (t.ex. 4rem eller 48px)"),
        ],
        "lists": [],
    },
}


# ---------------------------------------------------------------------------
# Value cleaning
# ---------------------------------------------------------------------------

_MAX = {"plain": 300, "text": 2000, "length": 20}


def _clean_link(raw):
    """Länkväljarens dolda input -> beskrivare. JSON valideras mot
    kind-vitlistan; en legacy-sträng uppgraderas via parse_href."""
    import json

    from apps.website.links import clean_descriptor, parse_href

    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return clean_descriptor(json.loads(raw))
    except (ValueError, TypeError):
        return clean_descriptor(parse_href(raw) or "") or ""


def _clean_value(spec, raw):
    kind = spec["type"]
    if kind in ("plain",):
        return sanitize_plain_text(raw or "", max_length=_MAX["plain"])
    if kind == "text":
        # Flerradigt fält: radbrytningar ska överleva (mallarna renderar dem
        # med linebreaksbr och schemats hjälptext lovar det).
        return sanitize_multiline_text(raw or "", max_length=_MAX["text"])
    if kind == "rich":
        return sanitize_rich_html_basic(raw or "")
    if kind == "url":
        try:
            return validate_url(raw or "")
        except ValidationError:
            return ""
    if kind == "image":
        try:
            return validate_media_id(raw or "")
        except ValidationError:
            return ""
    if kind == "faq_section":
        # Store as integer ID or empty string.
        try:
            v = int(raw) if raw else ""
            return v if v else ""
        except (ValueError, TypeError):
            return ""
    if kind == "choice":
        allowed = [c[0] for c in spec.get("choices", [])]
        if raw in allowed:
            return raw
        return allowed[0] if allowed else ""
    if kind == "length":
        v = (raw or "").strip()
        import re

        if re.fullmatch(r"\d+(\.\d+)?(px|rem|em|%)|var\(--[a-z0-9-]+\)", v):
            return v
        return ""
    if kind == "range":
        # Numeric value between 0 and 1 (stored as string for JSON compat)
        try:
            v = float(raw or "0")
            v = max(0.0, min(1.0, v))
            # Round to 2 decimals, strip trailing zeros
            return f"{v:.2f}".rstrip("0").rstrip(".")
        except (ValueError, TypeError):
            return ""
    return sanitize_plain_text(raw or "")


def _set_nested(data, key, value):
    if "." in key:
        head, tail = key.split(".", 1)
        data.setdefault(head, {})
        _set_nested(data[head], tail, value)
    else:
        data[key] = value


def _get_nested(data, key, default=""):
    cur = data
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def clean_block_data(block_type, post):
    """Rebuild + sanitise a block's `data` dict from POST for its type."""
    schema = BLOCK_EDIT_SCHEMA.get(block_type)
    if schema is None:
        return {}

    data = {}
    for spec in schema["fields"]:
        if spec["type"] == "link":
            value = _clean_link(post.get(spec["key"], ""))
        else:
            value = _clean_value(spec, post.get(spec["key"], ""))
        _set_nested(data, spec["key"], value)

    for lst in schema.get("lists", []):
        data[lst["key"]] = _collect_rows(lst, post)

    return data


def field_keys(block_type):
    """Every editable field key for a block type ("link.label" style included)."""
    schema = BLOCK_EDIT_SCHEMA.get(block_type)
    return [spec["key"] for spec in schema["fields"]] if schema else []


def list_specs(block_type):
    """The repeating-row declarations for a block type ([] if it has none)."""
    schema = BLOCK_EDIT_SCHEMA.get(block_type)
    return list(schema.get("lists", [])) if schema else []


def clean_block_rows(block_type, current, lists):
    """
    Sanitise repeating-row content ({list_key: [row, ...]}) onto a block's data.

    The counterpart to ``clean_block_values`` for the half of the schema that
    lives in ``lists``. Callers that hold rows rather than a POST (the AI
    operations) had no way in at all before this: ``clean_block_values`` only
    ever looked at ``fields``, so a block whose content is entirely rows -
    chips, marquee, contact_cards - could only be created empty, and every
    other list (steps, plans, quotes, ...) was unreachable.

    A row is a dict of sub-field keys, or a plain string for ``simple`` lists.
    Rows whose first sub-field is blank are dropped, exactly as the form path
    does. Unknown list keys or unknown sub-keys raise KeyError so the caller
    can turn it into a validation error the model can act on.
    """
    schema = BLOCK_EDIT_SCHEMA.get(block_type)
    if schema is None:
        raise KeyError(block_type)

    declared = {lst["key"]: lst for lst in schema.get("lists", [])}
    unknown = set(lists) - set(declared)
    if unknown:
        raise KeyError(", ".join(sorted(unknown)))

    data = copy.deepcopy(current or {})
    for key, rows in lists.items():
        lst = declared[key]
        if not isinstance(rows, list):
            raise KeyError(f"{key} (måste vara en lista av rader)")
        data[key] = _clean_rows(lst, rows)
    return data


def _clean_rows(lst, rows):
    """Sanitise one list's rows against its sub-field specs."""
    specs = {f["key"]: f for f in lst["fields"]}
    primary = lst["fields"][0]["key"]
    simple = lst.get("simple", False)

    cleaned = []
    for row in rows:
        if simple:
            # A simple list stores plain strings; accept {"<primary>": v} too
            # so the model can use one consistent row shape everywhere.
            raw = row.get(primary, "") if isinstance(row, dict) else row
            value = _clean_value(lst["fields"][0], raw)
            if str(value).strip():
                cleaned.append(value)
            continue

        if not isinstance(row, dict):
            raise KeyError(f"{lst['key']} (varje rad måste vara ett objekt)")
        unknown = set(row) - set(specs)
        if unknown:
            raise KeyError(", ".join(f"{lst['key']}.{k}" for k in sorted(unknown)))
        if not str(row.get(primary, "")).strip():
            continue  # skip empty rows, same rule as the form path
        out = {}
        for f in lst["fields"]:
            raw = row.get(f["key"], "")
            out[f["key"]] = _clean_link(raw) if f["type"] == "link" else _clean_value(f, raw)
        cleaned.append(out)
    return cleaned


def clean_block_values(block_type, current, values):
    """
    Sanitise a partial `{key: value}` update on top of a block's current data.

    The POST-driven `clean_block_data` above rebuilds the whole dict from a
    form; callers that only hold a few fields (the AI operations) need a
    partial merge instead. Same per-field sanitisers either way, so the
    security boundary is identical.

    Unknown keys raise KeyError - the caller turns that into a validation
    error the model can act on, rather than silently dropping the value.
    """
    schema = BLOCK_EDIT_SCHEMA.get(block_type)
    if schema is None:
        raise KeyError(block_type)

    specs = {spec["key"]: spec for spec in schema["fields"]}
    unknown = set(values) - set(specs)
    if unknown:
        raise KeyError(", ".join(sorted(unknown)))

    data = copy.deepcopy(current or {})
    for key, raw in values.items():
        spec = specs[key]
        # Link fields must go through _clean_link, exactly as the POST path
        # does. Without this they fell through to plain-text sanitising and
        # were stored as raw strings, so an AI-set link never became the
        # page-FK descriptor that survives a slug change (and never got the
        # dead-target handling in links.resolve_link).
        value = _clean_link(raw) if spec["type"] == "link" else _clean_value(spec, raw)
        _set_nested(data, key, value)
    return data


def _collect_rows(lst, post):
    subkeys = [f["key"] for f in lst["fields"]]
    arrays = {sk: post.getlist(f"{lst['key']}__{sk}") for sk in subkeys}
    count = max((len(v) for v in arrays.values()), default=0)
    primary = lst["fields"][0]["key"]
    simple = lst.get("simple", False)

    def cell(sk, i):
        return arrays[sk][i] if i < len(arrays.get(sk, [])) else ""

    rows = []
    for i in range(count):
        if not str(cell(primary, i)).strip():
            continue  # skip empty rows
        if simple:
            rows.append(_clean_value(lst["fields"][0], cell(primary, i)))
        else:
            row = {}
            for f in lst["fields"]:
                if f["type"] == "link":
                    row[f["key"]] = _clean_link(cell(f["key"], i))
                else:
                    row[f["key"]] = _clean_value(f, cell(f["key"], i))
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Build render context for the edit template
# ---------------------------------------------------------------------------


def build_form_context(block):
    """Produce a template-friendly structure of fields + lists with values."""
    schema = BLOCK_EDIT_SCHEMA.get(block.block_type)
    if schema is None:
        return None
    data = block.data or {}

    def enrich(spec, value):
        return {**spec, "value": value}

    fields = []
    for spec in schema["fields"]:
        fields.append(enrich(spec, _get_nested(data, spec["key"], "")))

    lists = []
    for lst in schema.get("lists", []):
        simple = lst.get("simple", False)
        stored = data.get(lst["key"], []) or []
        rows = []
        for item in stored:
            if simple:
                rows.append([{**lst["fields"][0], "value": item}])
            else:
                rows.append([enrich(f, item.get(f["key"], "")) for f in lst["fields"]])
        lists.append(
            {
                "key": lst["key"],
                "label": lst["label"],
                "singular": lst.get("singular", "rad"),
                "simple": simple,
                "fields": lst["fields"],
                "rows": rows,
            }
        )

    return {"type_label": schema["label"], "fields": fields, "lists": lists}
