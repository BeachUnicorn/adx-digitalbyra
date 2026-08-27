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
        "fields": [
            _f(
                "kicker",
                "plain",
                "Kicker (liten rad ovanför)",
                help="T.ex. Digitalbyrå — Stockholm. Hakparenteserna ritas av designen.",
            ),
            _f("title", "text", "Rubrik", help="Radbrytningar behålls."),
            _f("lead", "text", "Ingress"),
            _f("primary.label", "plain", "Primär knapp – text"),
            _f("primary.url", "link", "Primär knapp – länk"),
            _f("secondary.label", "plain", "Sekundär knapp – text"),
            _f("secondary.url", "link", "Sekundär knapp – länk"),
            _f("image_id", "image", "Hero-bild (valfri)"),
        ],
        "lists": [],
    },
    "chips": {
        "label": "Nyckeltal (chips)",
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
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("intro", "text", "Introtext"),
        ],
        "lists": [],
    },
    "case": {
        "label": "Case",
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
        "fields": [
            _f("label", "plain", "Text (vänster)"),
            _f("link.label", "plain", "Länktext"),
            _f("link.url", "link", "Länkadress"),
        ],
        "lists": [],
    },
    "split": {
        "label": "Bild + text",
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
                    _f("features", "text", "Punkter – en per rad"),
                    _f("cta_label", "plain", "Knapptext"),
                    _f("cta_url", "link", "Knapplänk"),
                ],
            },
        ],
    },
    "compare": {
        "label": "Jämförelsetabell",
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
                    _f("v1", "plain", "Värde 1 (+ / – / text)"),
                    _f("v2", "plain", "Värde 2"),
                    _f("v3", "plain", "Värde 3"),
                ],
            },
        ],
    },
    "team": {
        "label": "Team",
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
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("faq_section_id", "faq_section", "FAQ-sektion"),
        ],
        "lists": [],
    },
    "prose": {
        "label": "SEO-text",
        "fields": [
            _f("title", "plain", "Rubrik"),
            _f("body", "rich", "Text"),
        ],
        "lists": [],
    },
    "related": {
        "label": "Relaterade länkar",
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
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("intro", "text", "Introtext"),
        ],
        "lists": [],
    },
    "newsletter": {
        "label": "Nyhetsbrev",
        "fields": [
            _f("kicker", "plain", "Kicker"),
            _f("title", "plain", "Rubrik"),
            _f("note", "plain", "Not (under fältet)"),
        ],
        "lists": [],
    },
    "spacer": {
        "label": "Mellanrum",
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


def link_choices():
    """Valen i länkväljaren: sidorna, städerna och specialmålen. Byggs ur
    databasen vid varje rendering så listan aldrig ruttnar."""
    from apps.areas.models import Area
    from apps.website.models import BlockPage

    pages = [(f"page:{p.pk}", p.title) for p in BlockPage.objects.order_by("order", "title")]
    areas = [
        (f"area:{a.pk}", a.name)
        for a in Area.objects.filter(is_active=True).order_by("order", "name")
    ]
    special = [
        ("areas_index", "Stadsöversikten"),
        ("email", "Sajtens e-post"),
        ("phone", "Sajtens telefon"),
    ]
    return [("Sidor", pages), ("Städer", areas), ("Övrigt", special)]


def _clean_link(select_raw, custom_raw):
    """Länkväljarens två fält -> beskrivare. Fritextfältet vinner om ifyllt
    (extern adress eller egen intern rutt); annars gäller select-valet."""
    from apps.website.links import parse_href

    custom = sanitize_plain_text(custom_raw or "", max_length=500).strip()
    if custom:
        try:
            validate_url(custom)
        except ValidationError:
            return ""
        return parse_href(custom) or ""

    value = (select_raw or "").strip()
    if not value:
        return ""
    if value in ("areas_index", "email", "phone"):
        return {"kind": value}
    if ":" in value:
        kind, _, raw_id = value.partition(":")
        if kind in ("page", "area") and raw_id.isdigit():
            return {"kind": kind, "id": int(raw_id)}
    return ""


def _link_form_values(stored):
    """Lagrad beskrivare (eller legacy-sträng) -> (select-värde, fritext)."""
    if isinstance(stored, str) and stored:
        return "", stored
    if not isinstance(stored, dict):
        return "", ""
    kind = stored.get("kind", "")
    if kind in ("page", "area"):
        return f"{kind}:{stored.get('id')}", ""
    if kind in ("areas_index", "email", "phone"):
        return kind, ""
    if kind == "external":
        return "", stored.get("url", "")
    if kind == "path":
        return "", stored.get("path", "")
    return "", ""


def _clean_value(spec, raw):
    kind = spec["type"]
    if kind in ("plain",):
        return sanitize_plain_text(raw or "", max_length=_MAX["plain"])
    if kind == "text":
        return sanitize_plain_text(raw or "", max_length=_MAX["text"])
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
            value = _clean_link(post.get(spec["key"], ""), post.get(spec["key"] + "__custom", ""))
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
        _set_nested(data, key, _clean_value(specs[key], raw))
    return data


def _collect_rows(lst, post):
    subkeys = [f["key"] for f in lst["fields"]]
    arrays = {sk: post.getlist(f"{lst['key']}__{sk}") for sk in subkeys}
    # Länkfält bär två parallella inputs per rad (select + fritext).
    link_keys = [f["key"] for f in lst["fields"] if f["type"] == "link"]
    for lk in link_keys:
        arrays[lk + "__custom"] = post.getlist(f"{lst['key']}__{lk}__custom")
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
                    row[f["key"]] = _clean_link(cell(f["key"], i), cell(f["key"] + "__custom", i))
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
        entry = {**spec, "value": value}
        if spec["type"] == "link":
            select_value, custom_value = _link_form_values(value)
            entry.update(
                link_choices=link_choices(),
                value_select=select_value,
                value_custom=custom_value,
            )
        return entry

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
