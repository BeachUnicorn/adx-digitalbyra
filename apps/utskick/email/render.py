"""
Brev som HTML (README F.4, D9, H.5).

Stilen är bara Brev (mockups/flamingo-epost-brev.html): tabeller, inline-
stilar från style.brev_styles, 560 px, kolumner som staplas på mobilen utan
mediefrågor (inline-block med max-width), en <style> med @media
(max-width:620px) bara som förfining, <!--[if mso]>-ram för Outlook,
knappar som tabellceller med bgcolor (ingen VML), vita bakgrunder på varje
cell (mörkt läge), bilder alltid med width, height, alt och display:block,
inga externa typsnitt, inga relativa adresser, inga skript. Mallarna:
templates/utskick/brev/layout.html, _header.html, _footer.html och
blocks/<key>.html (utanför templates/flamingo/, så "inget style="-vakten
gäller inte där; test_s3_render har en egen vakt).

    EDITOR, PREVIEW, SEND = "editor", "preview", "send"
    @dataclass RenderContext
        utskick, account, mode, recipient (eller None), merge (värdena),
        fallbacks, basis (mottagarens grund), tracking_ok, test (testmejl),
        snapshot (underlaget: fryst eller färskt), palette, S, links
        ({"<block_id>:<plats>": TrackedLink-id}), web (webbversionen),
        address (mottagarens e-post, för sidfotens länkar)
    context_for(utskick, *, mode, recipient=None, contact=None, test=False,
                snapshot=None) -> RenderContext
                                     en mottagare (send), en kontakt (preview och test)
                                     eller ingen (editor). snapshot med "blocks" är ett
                                     fryst mejl (Utskick.email_snapshot); utan "blocks"
                                     byggs underlaget nu och snapshot["links"] används
                                     (testmejl med riktiga TrackedLink)
    render_html(utskick, ctx, mode=None) -> str
                                     hela mejlet: förhandstexten, sidhuvudet med "Visa i
                                     webbläsaren" och loggan, blocken, sidfoten med
                                     namnet, adressen och telefonen (en per rad), "Ändra vad du
                                     får · Avregistrera dig · Visa i webbläsaren · Så
                                     hanterar <företaget> dina uppgifter"; varje spårad
                                     href genom links.email_url när ctx.links har platsen
                                     (mailto:, tel: och våra egna länkar aldrig); pixeln
                                     (links.pixel_url) bara i send när utskicket har
                                     open_tracking och mottagaren tracking_ok (H.5)
    render_block(utskick, block, *, ctx=None) -> str
                                     ett block (<tr>) i läget editor, med {% pb %}-attributen
    snapshot(utskick, *, now=None) -> dict
                                     frysningens underlag (Utskick.email_snapshot):
                                     blocken (aktiva fält), accent och logga, Företagets
                                     uppgifter, omdömena, bildernas absoluta adresser och
                                     EmailImage-id. Frysningen lägger till
                                     {"links": {"<block_id>:<plats>": link_id}}
    collect_links(utskick, doc=None) -> list[LinkSpot]
                                     (block_id, plats, adress, etikett) som frysningen gör
                                     till TrackedLink; samma ordning som renderingen
    html_size(utskick) -> int        byte för de längsta värdena och personliga länkar
                                     (checks: under 102 kB, F.5)
    web_view(utskick, recipient=None) -> str
                                     webbversionen (/w/): samma mejl utan pixeln och utan
                                     "Visa i webbläsaren"
    calendar_ics(utskick, block_id) -> str | None
                                     händelseblocket som text/calendar (/c/)
    subject_for(utskick, ctx) -> str, preheader_for(utskick, ctx) -> str
                                     sammanfogade, en rad, högst 60 tecken per värde (F.3)

Underlaget ("data") är samma i alla lägen: snapshot() bygger det vid
frysningen och sparar det, redigeraren och förhandsvisningen bygger det nu
(build_data). Därför ritar webbversionen och varje mottagare samma mejl även
om Företaget eller loggan ändras efter frysningen.

Platserna: varje spårad länk (http och https) i ett block får en plats i
den ordning blocket ritar dem (Linker). collect_links kör samma kod, så
frysningens TrackedLink och renderingens länkar stämmer. Sammanfogningen
(composer.merge) görs på texterna efter att rich_basic tolkats: ett värde
blir aldrig en länk eller fetstil, och värdena escapas av mallarna.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from urllib.parse import quote_plus

from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from . import blocks, style
from .registry import EMAIL_TYPES, MEDIA

EDITOR = "editor"
PREVIEW = "preview"
SEND = "send"
MODES = (EDITOR, PREVIEW, SEND)

LAYOUT_TEMPLATE = "utskick/brev/layout.html"
HEADER_TEMPLATE = "utskick/brev/_header.html"
FOOTER_TEMPLATE = "utskick/brev/_footer.html"
BLOCK_TEMPLATE = "utskick/brev/blocks/{type}.html"
#: Redigeringslägets dokument (srcdoc i redigeraren): inga skript alls.
EDITING_CSP = '<meta http-equiv="Content-Security-Policy" content="script-src \'none\'">'
#: Underlagets version (snapshot).
DATA_VERSION = 1

WEB_VIEW_TEXT = "Visa i webbläsaren"
PREFERENCES_TEXT = "Ändra vad du får"
UNSUBSCRIBE_TEXT = "Avregistrera dig"
CALENDAR_TEXT = "Lägg till i kalendern"
MAP_TEXT = "Hitta hit"
IMAGE_MISSING_TEXT = "Välj en bild"

#: Bildernas syfte per fält (images.PURPOSES).
PURPOSES = {
    ("hero", "image"): "content",
    ("image", "image"): "content",
    ("image_text", "image"): "content",
    ("gallery", "image"): "content",
    ("hours", "map_image"): "content",
    ("video", "thumbnail"): "video",
    ("person", "photo"): "avatar",
    ("signature", "photo"): "avatar",
}
#: Visad bredd per bildplats (px); filen är upp till dubbelt så bred.
CONTENT = style.CONTENT
GALLERY_GAP = 10
GALLERY_WIDTH = (CONTENT - GALLERY_GAP) // 2
SIDE_GAP = 18
SIDE_IMAGE = 204
SIDE_TEXT = CONTENT - SIDE_IMAGE - SIDE_GAP
HALF_GAP = 14
HALF = (CONTENT - HALF_GAP) // 2
COLUMN_GAP = 16
SPACER = {"s": 8, "m": 16, "l": 32}
#: Längsta citat ur ett omdöme i mejlet.
QUOTE_MAX = 300
#: Förhandstextens utfyllnad: e-postprogrammen visar annars mejlets första
#: text efter förhandstexten.
PREHEADER_PAD = "&#847;&zwnj;&nbsp;" * 40

MONTHS = (
    "januari",
    "februari",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "augusti",
    "september",
    "oktober",
    "november",
    "december",
)
MONTHS_SHORT = ("JAN", "FEB", "MAR", "APR", "MAJ", "JUN", "JUL", "AUG", "SEP", "OKT", "NOV", "DEC")
WEEKDAYS = ("Måndag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lördag", "Söndag")
NBSP = chr(0xA0)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datum och tider (F.1 date, time)
# ---------------------------------------------------------------------------


def parse_date(value):
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def date_long(value, *, year=None):
    """ "2026-10-24" som "24 oktober" (med året när det inte är year)."""
    day = parse_date(value) if not isinstance(value, date) else value
    if day is None:
        return ""
    text = f"{day.day} {MONTHS[day.month - 1]}"
    if year is not None and day.year != year:
        text += f" {day.year}"
    return text


def time_text(value):
    """ "15:00" som "15", "15:30" som "15.30" (I.3)."""
    text = str(value or "")
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        return ""
    hour, minute = text.split(":")
    return str(int(hour)) if minute == "00" else f"{int(hour)}.{minute}"


def event_line(fields, *, year=None):
    """ "Fredag 24 oktober kl. 15 till 18" (utan slut: "kl. 15"; utan start
    bara datumet)."""
    day = parse_date(fields.get("date"))
    if day is None:
        return ""
    text = f"{WEEKDAYS[day.weekday()]} {date_long(day, year=year)}"
    start, end = time_text(fields.get("start")), time_text(fields.get("end"))
    if start:
        text += f" kl. {start}"
        if end:
            text += f" till {end}"
    return text


# ---------------------------------------------------------------------------
# Kontexten
# ---------------------------------------------------------------------------


@dataclass
class RenderContext:
    utskick: object
    account: object
    mode: str
    recipient: object = None
    merge: dict = field(default_factory=dict)
    fallbacks: dict = field(default_factory=dict)
    basis: str = ""
    tracking_ok: bool = False
    test: bool = False
    snapshot: dict = field(default_factory=dict)
    palette: object = None
    S: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    web: bool = False
    address: str = ""

    @property
    def editing(self):
        return self.mode == EDITOR

    def m(self, text):
        """Texten sammanfogad (F.3); i redigeraren som den står."""
        if self.editing:
            return str(text or "")
        from .. import composer

        return composer.merge(str(text or ""), self.merge, self.fallbacks)

    def resolve(self, block_id, position, url):
        link_id = self.links.get(f"{block_id}:{position}") if self.links else None
        if link_id is None:
            return url
        from .. import links

        return links.email_url(self.utskick, self.recipient, link_id)


@dataclass(frozen=True)
class LinkSpot:
    block_id: str
    position: int
    url: str
    label: str = ""


class Linker:
    """Ett blocks spårade länkar i den ordning blocket ritar dem. Bara
    http(s) får en plats; mailto: och tel: går rakt igenom."""

    def __init__(self, ctx, block_id, record=None):
        self.ctx = ctx
        self.block_id = block_id
        self.record = record
        self.n = 0

    def __call__(self, url, label=""):
        url = str(url or "").strip()
        lowered = url.lower()
        if lowered.startswith(("mailto:", "tel:")):
            return url
        if not lowered.startswith(("http://", "https://")):
            # Bara http(s), mailto och tel blir länkar (blocks.clean_url); något
            # annat (ett osparat block i redigeraren) pekar på länkvärden.
            from .. import links

            return links.email_link_base() + "/"
        position = self.n
        self.n += 1
        if self.record is not None:
            self.record.append(LinkSpot(self.block_id, position, url, str(label or "")[:120]))
        if self.ctx is None:
            return url
        return self.ctx.resolve(self.block_id, position, url)


def _settings(account):
    from ..access import settings_for

    return settings_for(account)


def _consent_basis(contact):
    """Grunden för kontaktens e-post (förhandsvisningens sidfot)."""
    from ..models import CHANNEL_EMAIL, Consent

    if contact is None or not getattr(contact, "pk", None):
        return ""
    row = (
        Consent.objects.filter(contact=contact, channel=CHANNEL_EMAIL)
        .values_list("basis", flat=True)
        .first()
    )
    return row if row and row != Consent.Basis.NONE else ""


def context_for(utskick, *, mode, recipient=None, contact=None, test=False, snapshot=None):
    """RenderContext för ett mejl: en mottagare (send), en kontakt (preview
    och testmejl) eller ingen (redigeraren)."""
    from .. import composer

    if mode not in MODES:
        raise ValueError(f"Okänt läge: {mode}")
    account = utskick.account
    frozen = isinstance(snapshot, dict) and isinstance(snapshot.get("blocks"), list)
    if frozen:
        data = snapshot
    else:
        data = build_data(utskick, create=mode != EDITOR)
        if isinstance(snapshot, dict) and isinstance(snapshot.get("links"), dict):
            data["links"] = snapshot["links"]
    palette = style.palette_for_accent(data.get("accent"))
    merge, basis, tracking_ok, address = {}, "", False, ""
    if recipient is not None:
        merge = dict(recipient.merge or {}) if isinstance(recipient.merge, dict) else {}
        basis = recipient.basis or ""
        tracking_ok = bool(recipient.tracking_ok)
        address = recipient.address or ""
    elif contact is not None:
        merge = composer.merge_values(contact, composer.field_defs(account))
        basis = _consent_basis(contact)
        address = contact.email or ""
    fallbacks = utskick.merge_fallbacks if isinstance(utskick.merge_fallbacks, dict) else {}
    return RenderContext(
        utskick=utskick,
        account=account,
        mode=mode,
        recipient=recipient,
        merge=merge,
        fallbacks=fallbacks,
        basis=basis,
        tracking_ok=tracking_ok,
        test=test,
        snapshot=data,
        palette=palette,
        S=style.brev_styles(palette),
        links=dict(data.get("links") or {}),
        address=address,
    )


# ---------------------------------------------------------------------------
# Underlaget
# ---------------------------------------------------------------------------


def _decimal_text(value):
    if value is None:
        return ""
    try:
        return f"{float(value):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def _short(text, limit=QUOTE_MAX):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:")
    return f"{cut} ..."


def _reviews_data(account, fields):
    """Omdömena för ett omdömesblock: källan, betyget, antalet och de valda
    omdömena (högst count). None när källan inte går att visa."""
    from apps.flamingo import reco

    source = fields.get("source") or "google"
    try:
        count = max(1, min(3, int(fields.get("count") or 2)))
    except (TypeError, ValueError):
        count = 2
    if source == "reco":
        if not account.reco_trusted:
            return None
        rating = account.reco_rating
        total = account.reco_review_count
        if account.is_demo and rating is None:
            rating = 4.7
        rows = reco.selected_reviews(account)
        label = "Reco"
    else:
        if not account.google_profile_trusted:
            return None
        rating = account.trusted_google_rating
        total = account.google_review_count if rating is not None else None
        rows = account.selected_google_reviews()
        label = "Google"
    items = [
        {"text": _short(r.get("text")), "author": " ".join(str(r.get("author") or "").split())}
        for r in rows
        if isinstance(r, dict) and str(r.get("text") or "").strip()
    ][:count]
    if not items and rating is None:
        return None
    return {
        "source": source,
        "label": label,
        "rating": _decimal_text(rating),
        "rating_value": float(rating) if rating is not None else 0.0,
        "count": total,
        "items": items,
    }


def _image_info(asset, purpose, create):
    """{"id", "url", "width", "height", "alt"} för en bild i mejlet: en
    EmailImage (skapas när create), annars arkivets fil (bara redigeraren)."""
    from apps.flamingo import media

    from ..models import EmailImage
    from . import images

    row = None
    if create:
        try:
            row = images.rendition(asset, purpose)
        except media.MediaError:
            return None
    else:
        size = images.target_size(asset.width, asset.height, purpose)
        row = (
            EmailImage.objects.filter(asset=asset, purpose=purpose, width=size[0])
            .order_by("pk")
            .first()
        )
    if row is not None and row.file:
        return {
            "id": row.pk,
            "url": images.absolute_url(row),
            "width": row.width,
            "height": row.height,
            "alt": asset.alt or "",
        }
    if not asset.file:
        return None
    from apps.flamingo.exports import landing_base_url

    url = asset.file.url
    if not url.startswith(("https://", "http://")):
        url = landing_base_url().rstrip("/") + url
    return {"id": None, "url": url, "width": asset.width, "height": asset.height, "alt": asset.alt}


def _media_needs(entries):
    """{(asset_id, syfte)} för bilderna i blocken."""
    needs = set()
    for entry in entries:
        block_type = EMAIL_TYPES.get(entry["type"])
        fields = entry["fields"]
        for spec in block_type.fields:
            if spec.kind == MEDIA and isinstance(fields.get(spec.key), int):
                needs.add((fields[spec.key], PURPOSES.get((entry["type"], spec.key), "content")))
            elif spec.items:
                for sub in spec.items:
                    if sub.kind != MEDIA:
                        continue
                    for item in fields.get(spec.key) or []:
                        if isinstance(item, dict) and isinstance(item.get(sub.key), int):
                            purpose = PURPOSES.get((entry["type"], sub.key), "content")
                            needs.add((item[sub.key], purpose))
    return needs


def _company(account):
    from apps.flamingo.pagebuilder.facts import facts_for

    from ..capture import privacy_url

    row = _settings(account)
    facts = facts_for(account)
    customer = getattr(account, "customer", None)
    # Sidfoten bär det juridiska namnet ("Exempelrör AB", H.5), utan demots
    # markering.
    legal = re.sub(r"\s*\([^)]*\)", "", getattr(customer, "name", "") or "").strip()
    return {
        "name": legal or facts.company or row.display_name,
        "display_name": row.display_name,
        "address": facts.address,
        "phone": blocks.phone_e164(facts.phone),
        "phone_text": facts.phone,
        "hours": facts.hours,
        "privacy_url": privacy_url(account, row, absolute=True),
    }


def build_data(utskick, *, create=True, doc=None, now=None):
    """Underlaget för ett mejl som det ser ut nu (se modulens beskrivning).
    create: bildernas EmailImage skapas när de saknas (inte i redigeraren)."""
    from apps.flamingo.models import MediaAsset

    from . import images

    account = utskick.account
    entries = []
    for block, _block_type, fields in blocks.active_blocks(utskick, doc):
        version = blocks.active_version(block) or {}
        entries.append(
            {
                "id": str(block.get("id") or ""),
                "type": block.get("type"),
                "variant": block.get("variant") or "",
                "version": version.get("id", ""),
                "fields": fields,
            }
        )
    needs = _media_needs(entries)
    assets = {
        a.pk: a for a in MediaAsset.objects.filter(account=account, pk__in={i for i, _p in needs})
    }
    image_map, image_ids = {}, []
    for asset_id, purpose in sorted(needs):
        asset = assets.get(asset_id)
        info = _image_info(asset, purpose, create) if asset is not None else None
        if info is None:
            continue
        image_map[f"{asset_id}:{purpose}"] = info
        if info.get("id"):
            image_ids.append(info["id"])
    logo = None
    logo_position = utskick.logo_position or "left"
    if logo_position != "none":
        row = images.logo_for(account) if create else _existing_logo(account)
        if row is not None:
            logo = _logo_info(row)
            if logo.get("id"):
                image_ids.append(logo["id"])
    reviews = {}
    for entry in entries:
        if entry["type"] == "reviews":
            data = _reviews_data(account, entry["fields"])
            if data is not None:
                reviews[entry["id"]] = data
    return {
        "v": DATA_VERSION,
        "at": (now or timezone.now()).isoformat(),
        "accent": style.accent_for(utskick),
        "logo_position": logo_position if logo is not None else "none",
        "logo": logo,
        "blocks": entries,
        "company": _company(account),
        "images": image_map,
        "image_ids": image_ids,
        "reviews": reviews,
        "information": bool(utskick.is_information),
    }


def _existing_logo(account):
    """Loggans rendition om den redan finns, annars arkivets logga (bara
    redigeraren)."""
    from ..models import EmailImage
    from . import images

    logo = images.logo_asset(account)
    if logo is None:
        return None
    size = images.target_size(logo.width, logo.height, images.LOGO)
    row = EmailImage.objects.filter(asset=logo, purpose=images.LOGO, width=size[0]).first()
    return row if row is not None else logo


def _logo_info(row):
    """Loggan i sidhuvudet: högst 40 px hög och 220 bred med proportionerna
    kvar, aldrig större än filen (images.display_size)."""
    from apps.flamingo.models import MediaAsset

    from . import images

    if isinstance(row, MediaAsset):
        width, height, url, image_id, alt = row.width, row.height, None, None, row.alt
        from apps.flamingo.exports import landing_base_url

        url = row.file.url if row.file else ""
        if url and not url.startswith(("https://", "http://")):
            url = landing_base_url().rstrip("/") + url
    else:
        width, height, url, image_id = row.width, row.height, images.absolute_url(row), row.pk
        alt = row.asset.alt if row.asset_id and row.asset is not None else ""
    if not url or not width or not height:
        return None
    shown_width, shown_height = images.display_size(width, height)
    return {
        "id": image_id,
        "url": url,
        "width": shown_width,
        "height": shown_height,
        "alt": alt or "",
    }


def snapshot(utskick, *, now=None):
    """Frysningens underlag (Utskick.email_snapshot), utan länkarna:
    frysningen lägger till {"links": {...}} när TrackedLink finns."""
    return build_data(utskick, create=True, now=now)


# ---------------------------------------------------------------------------
# rich_basic som HTML och text
# ---------------------------------------------------------------------------


def _resolve_rich(ast, ctx, linker):
    """AST:n med sammanfogade textbitar och länkarnas href (en gång per
    länk, så att platserna stämmer)."""

    def plain(nodes):
        out = []
        for node in nodes:
            if node.get("t") == "text":
                out.append(node.get("v", ""))
            else:
                out.append(plain(node.get("c") or []))
        return "".join(out)

    def inline(nodes):
        out = []
        for node in nodes:
            kind = node.get("t")
            if kind == "text":
                out.append({"t": "text", "v": ctx.m(node.get("v", "")) if ctx else node.get("v")})
            elif kind == "br":
                out.append({"t": "br"})
            elif kind == "a":
                href = linker(node.get("href", ""), plain(node.get("c") or []))
                out.append({"t": "a", "href": href, "c": inline(node.get("c") or [])})
            elif kind in ("b", "i"):
                out.append({"t": kind, "c": inline(node.get("c") or [])})
        return out

    resolved = []
    for node in ast:
        if node.get("t") == "ul":
            resolved.append({"t": "ul", "items": [inline(i) for i in node.get("items") or []]})
        else:
            resolved.append({"t": "p", "c": inline(node.get("c") or [])})
    return resolved


def _inline_html(nodes, S):
    parts = []
    for node in nodes:
        kind = node.get("t")
        if kind == "text":
            parts.append(escape(node.get("v", "")))
        elif kind == "br":
            parts.append("<br>")
        elif kind == "b":
            parts.append(f"<strong>{_inline_html(node.get('c') or [], S)}</strong>")
        elif kind == "i":
            parts.append(f"<em>{_inline_html(node.get('c') or [], S)}</em>")
        elif kind == "a":
            parts.append(
                format_html(
                    '<a href="{}" style="{}">{}</a>',
                    node.get("href", ""),
                    S["link"],
                    mark_safe(_inline_html(node.get("c") or [], S)),
                )
            )
    return "".join(parts)


def rich_html(resolved, S, *, first_gap=False):
    """Stycken och listor med Brevs stilar; varje stycke efter det första
    får 12 px luft ovanför (som listan i mockupen)."""
    parts = []
    for n, node in enumerate(resolved):
        gap = "margin-top:12px;" if n or first_gap else ""
        if node.get("t") == "ul":
            items = "".join(
                f'<li style="{S["li"]}">{_inline_html(item, S)}</li>' for item in node["items"]
            )
            ul_style = (
                S["ul"] if n or first_gap else S["ul"].replace("margin:12px 0 0;", "margin:0;")
            )
            parts.append(f'<ul style="{ul_style}">{items}</ul>')
        else:
            parts.append(f'<p style="{S["p"]}{gap}">{_inline_html(node["c"], S)}</p>')
    return mark_safe("".join(parts))


def rich_inline_html(resolved, S):
    """Bara textraderna (steg): stycken med radbrytning emellan, utan <p>."""
    lines = []
    for node in resolved:
        if node.get("t") == "ul":
            lines.extend(_inline_html(item, S) for item in node["items"])
        else:
            lines.append(_inline_html(node["c"], S))
    return mark_safe("<br>".join(lines))


def _inline_text(nodes):
    parts = []
    for node in nodes:
        kind = node.get("t")
        if kind == "text":
            parts.append(node.get("v", ""))
        elif kind == "br":
            parts.append("\n")
        elif kind == "a":
            label = _inline_text(node.get("c") or [])
            href = node.get("href", "")
            parts.append(f"{label} ({href})" if href and href != label else label)
        else:
            parts.append(_inline_text(node.get("c") or []))
    return "".join(parts)


def rich_text(resolved):
    """Textversionen av rich_basic: stycken med en tom rad emellan,
    listor med "- ", länkar som "text (adress)"."""
    out = []
    for node in resolved:
        if node.get("t") == "ul":
            out.append("\n".join("- " + _inline_text(item) for item in node["items"]))
        else:
            out.append(_inline_text(node["c"]))
    return "\n\n".join(p for p in out if p.strip())


# ---------------------------------------------------------------------------
# Blocken
# ---------------------------------------------------------------------------


def _image(data, asset_id, purpose, shown_width):
    """En bild med visad bredd och höjd, eller None."""
    if not isinstance(asset_id, int):
        return None
    info = (data.get("images") or {}).get(f"{asset_id}:{purpose}") if data else None
    if not info or not info.get("url") or not info.get("width") or not info.get("height"):
        return None
    width = min(shown_width, int(info["width"]))
    if purpose == "content" and int(info["width"]) >= shown_width:
        width = shown_width
    height = max(1, round(width * int(info["height"]) / int(info["width"])))
    return {"url": info["url"], "width": width, "height": height, "alt": info.get("alt") or ""}


def _initials(name):
    parts = [p for p in re.split(r"[\s.]+", str(name or "")) if p]
    return "".join(p[0] for p in parts[:2]).upper()


def _phone(value):
    from ..normalize import display_phone

    return display_phone(value) if value else ""


def maps_url(address):
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(address)


_HOURS_SPLIT = re.compile(r"\s*[;\n]\s*|,\s+(?=[A-ZÅÄÖ])")
_HOURS_ROW = re.compile(r"^(.*?[^\d\s])\s+((?:\d|kl\.?\s|stängt|stängd|dygnet).*)$", re.I)


def hours_rows(text):
    """Öppettiderna som rader (etikett, tid): "Mån till fre 07 till 16;
    lör och sön stängt" blir två rader."""
    rows = []
    for part in _HOURS_SPLIT.split(str(text or "").strip()):
        part = part.strip().rstrip(".")
        if not part:
            continue
        match = _HOURS_ROW.match(part)
        rows.append((match.group(1), match.group(2)) if match else (part, ""))
    return rows[:8]


def _stars(rating_value):
    return "★" * max(1, min(5, round(rating_value or 0))) if rating_value else ""


def _prepare(kind, f, data, ctx, linker, editing):
    """Blockets vy: det mallen och textversionen ritar. hidden: blocket
    ritas inte (utanför redigeraren). Länkarna begärs av linker i den
    ordning blocket ritar dem, och bara för det som ritas."""
    m = ctx.m if ctx is not None else (lambda text: str(text or ""))
    S = ctx.S if ctx is not None else style.brev_styles(style.palette_for_accent(None))
    data = data or {}
    v = {"hidden": False}
    if kind == "hero":
        v["image"] = _image(data, f.get("image"), "content", CONTENT)
        v["kicker"] = m(f.get("kicker"))
        v["title"] = m(f.get("title"))
        v["lead"] = m(f.get("lead"))
        v["button_text"] = m(f.get("button_text"))
        v["hidden"] = not (v["title"] or v["lead"] or v["image"])
        if v["hidden"] and not editing:
            return v
        if f.get("button_text") and f.get("button_url"):
            v["button_href"] = linker(f["button_url"], f.get("button_text"))
    elif kind == "heading":
        v["text"] = m(f.get("text"))
        v["tag"] = "h3" if f.get("size") == "h3" else "h2"
        v["style"] = S["h3"] if v["tag"] == "h3" else S["h2"]
        v["hidden"] = not v["text"]
    elif kind in ("text", "callout"):
        key = "body" if kind == "text" else "text"
        ast = blocks.parse_rich(f.get(key), bold_only=kind == "callout")
        v["hidden"] = not ast
        if v["hidden"] and not editing:
            return v
        v["rich"] = _resolve_rich(ast, ctx, linker)
        v["html"] = rich_html(v["rich"], S)
    elif kind == "button":
        v["align"] = "center" if f.get("align") == "center" else "left"
        v["hidden"] = not (f.get("primary_text") and f.get("primary_url"))
        if v["hidden"] and not editing:
            return v
        v["buttons"] = []
        if f.get("primary_text") and f.get("primary_url"):
            v["primary"] = {
                "text": m(f["primary_text"]),
                "href": linker(f["primary_url"], f["primary_text"]),
            }
        if f.get("secondary_text") and f.get("secondary_url"):
            v["secondary"] = {
                "text": m(f["secondary_text"]),
                "href": linker(f["secondary_url"], f["secondary_text"]),
            }
    elif kind == "image":
        v["image"] = _image(data, f.get("image"), "content", CONTENT)
        v["caption"] = m(f.get("caption"))
        v["hidden"] = v["image"] is None
        if v["hidden"] and not editing:
            return v
        if f.get("url") and v["image"] is not None:
            v["href"] = linker(f["url"], f.get("caption") or (v["image"] or {}).get("alt"))
    elif kind == "image_text":
        v["image"] = _image(data, f.get("image"), "content", SIDE_IMAGE)
        v["title"] = m(f.get("title"))
        v["body"] = m(f.get("body"))
        v["right"] = f.get("side") == "right"
        v["hidden"] = not (v["image"] or v["title"] or v["body"])
        if v["hidden"] and not editing:
            return v
        if f.get("link_text") and f.get("link_url"):
            v["link_text"] = m(f["link_text"])
            v["link_href"] = linker(f["link_url"], f["link_text"])
        v["image_width"], v["text_width"] = SIDE_IMAGE + SIDE_GAP, SIDE_TEXT
    elif kind == "columns":
        items = [i for i in f.get("items") or [] if isinstance(i, dict)][:3]
        n = max(1, len(items))
        inner = (CONTENT - COLUMN_GAP * (n - 1)) // n
        v["columns"] = []
        for i, item in enumerate(items):
            last = i == len(items) - 1
            v["columns"].append(
                {
                    "number": m(item.get("number")),
                    "title": m(item.get("title")),
                    "text": m(item.get("text")),
                    "width": inner if last else inner + COLUMN_GAP,
                    "pad": 0 if last else COLUMN_GAP,
                    "index": i,
                }
            )
        v["hidden"] = not items
    elif kind == "divider":
        pass
    elif kind == "offer":
        valid = parse_date(f.get("valid_until"))
        v["valid"] = f"Till {date_long(valid, year=timezone.localdate().year)}" if valid else ""
        v["title"] = m(f.get("title"))
        v["text"] = m(f.get("text"))
        v["code"] = f.get("code") or ""
        v["hidden"] = not (v["title"] or v["code"] or v["text"])
    elif kind == "prices":
        rows = [
            {"name": m(i.get("name")), "price": m(i.get("price"))}
            for i in f.get("items") or []
            if isinstance(i, dict)
        ]
        for n, row in enumerate(rows):
            row["last"] = n == len(rows) - 1
            row["index"] = n
        v["rows"] = rows
        v["title"] = m(f.get("title"))
        v["note"] = m(f.get("note"))
        v["hidden"] = not rows
    elif kind == "reviews":
        review = (data.get("reviews") or {}).get(ctx_block_id(linker)) if data else None
        v["hidden"] = review is None
        if review is not None:
            v["summary"] = f.get("show_summary") != "no" and bool(review.get("rating"))
            v["stars"] = _stars(review.get("rating_value"))
            v["rating"] = review.get("rating") or ""
            count = review.get("count")
            v["count_text"] = (
                f"{_tal(count)} omdömen på {review.get('label')}"
                if count
                else f"på {review.get('label')}"
            )
            v["source_label"] = review.get("label") or ""
            v["quotes"] = list(review.get("items") or [])
            v["hidden"] = not (v["summary"] or v["quotes"])
    elif kind == "steps":
        steps = []
        for item in f.get("items") or []:
            if not isinstance(item, dict):
                continue
            ast = blocks.parse_rich(item.get("text"), bold_only=True)
            if not ast:
                continue
            resolved = _resolve_rich(ast, ctx, linker)
            steps.append({"rich": resolved, "html": rich_inline_html(resolved, S)})
        for n, step in enumerate(steps, start=1):
            step["n"] = n
            step["last"] = n == len(steps)
        v["steps"] = steps
        v["title"] = m(f.get("title"))
        v["hidden"] = not steps
    elif kind == "event":
        day = parse_date(f.get("date"))
        v["hidden"] = day is None
        if v["hidden"] and not editing:
            return v
        year = timezone.localdate().year
        v["month"] = MONTHS_SHORT[day.month - 1] if day else ""
        v["day"] = str(day.day) if day else ""
        v["line"] = event_line(f, year=year)
        v["title"] = m(f.get("title"))
        v["place"] = m(f.get("place"))
        utskick = getattr(ctx, "utskick", None)
        if f.get("calendar") != "no" and day is not None and getattr(utskick, "pk", None):
            from .. import links

            v["calendar_href"] = links.calendar_url(utskick.pk, ctx_block_id(linker))
    elif kind == "person":
        v["photo"] = _image(data, f.get("photo"), "avatar", 56)
        v["name"] = m(f.get("name"))
        v["role"] = m(f.get("role"))
        v["initials"] = _initials(f.get("name"))
        v["phone"] = _phone(f.get("phone"))
        v["tel"] = f"tel:{f['phone']}" if f.get("phone") else ""
        v["email"] = f.get("email") or ""
        v["mailto"] = f"mailto:{f['email']}" if f.get("email") else ""
        v["hidden"] = not v["name"]
    elif kind == "video":
        v["image"] = _image(data, f.get("thumbnail"), "video", CONTENT)
        v["title"] = m(f.get("title"))
        v["hidden"] = v["image"] is None or not f.get("url")
        if v["hidden"] and not editing:
            return v
        if f.get("url"):
            v["href"] = linker(f["url"], f.get("title") or "Video")
    elif kind == "gallery":
        cells = []
        for index, item in enumerate(f.get("items") or []):
            if not isinstance(item, dict):
                continue
            image = _image(data, item.get("image"), "content", GALLERY_WIDTH)
            if image is None and not editing:
                continue
            if image is not None and item.get("alt"):
                image["alt"] = m(item["alt"])
            cells.append({"image": image, "index": index, "path": f"items.{index}.image"})
        rows = [cells[i : i + 2] for i in range(0, len(cells), 2)]
        for n, row in enumerate(rows):
            for cell in row:
                cell["last_row"] = n == len(rows) - 1
        v["rows"] = rows
        v["cell_width"] = GALLERY_WIDTH
        v["hidden"] = not cells
    elif kind == "faq":
        items = [
            {"q": m(i.get("q")), "a": m(i.get("a"))}
            for i in f.get("items") or []
            if isinstance(i, dict) and (i.get("q") or i.get("a"))
        ]
        for n, item in enumerate(items):
            item["last"] = n == len(items) - 1
            item["index"] = n
        v["items"] = items
        v["title"] = m(f.get("title"))
        v["hidden"] = not items
    elif kind == "hours":
        company = data.get("company") or {}
        rows = hours_rows(company.get("hours")) if f.get("show_hours") != "no" else []
        address = company.get("address") if f.get("show_address") != "no" else ""
        v["rows"] = [
            {"label": label, "value": value, "last": n == len(rows) - 1}
            for n, (label, value) in enumerate(rows)
        ]
        v["address"] = address or ""
        v["map"] = _image(data, f.get("map_image"), "content", HALF)
        v["hidden"] = not (v["rows"] or v["address"])
        if v["hidden"] and not editing:
            return v
        if address:
            v["map_text"] = m(f.get("map_text")) or MAP_TEXT
            v["map_href"] = linker(maps_url(address), v["map_text"])
            if v["map"] is not None:
                v["map_image_href"] = v["map_href"]
        v["half"] = HALF
        v["half_pad"] = HALF + HALF_GAP
    elif kind == "signature":
        v["greeting"] = m(f.get("greeting"))
        v["script_name"] = m(f.get("script_name"))
        v["photo"] = _image(data, f.get("photo"), "avatar", 48)
        v["name"] = m(f.get("name"))
        v["line"] = m(f.get("line"))
        v["initials"] = _initials(f.get("name"))
        v["phone"] = _phone(f.get("phone"))
        v["tel"] = f"tel:{f['phone']}" if f.get("phone") else ""
        v["hidden"] = not (v["greeting"] or v["script_name"] or v["name"])
    elif kind == "spacer":
        v["height"] = SPACER.get(f.get("size") or "m", SPACER["m"])
    elif kind == "social":
        from .registry import SOCIAL_LABELS

        links_out = []
        for item in f.get("items") or []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            label = SOCIAL_LABELS.get(item.get("network"), "Länk")
            links_out.append({"label": label, "href": linker(item["url"], label)})
        for n, link in enumerate(links_out):
            link["last"] = n == len(links_out) - 1
        v["links"] = links_out
        v["hidden"] = not links_out
    return v


def ctx_block_id(linker):
    return linker.block_id if linker is not None else ""


def _tal(value):
    from apps.flamingo.templatetags.flamingo_app import tal

    return tal(value)


def _block_context(entry, ctx, data, editing, record=None):
    """(BlockType, vy, mallens kontext) för ett block, eller None."""
    block_type = EMAIL_TYPES.get(entry.get("type"))
    if block_type is None:
        return None
    raw = entry.get("fields") or {}
    fields = {spec.key: raw.get(spec.key, spec.empty()) for spec in block_type.fields}
    linker = Linker(ctx, entry.get("id") or "", record)
    view = _prepare(block_type.key, fields, data, ctx, linker, editing)
    context = {
        "b": {
            "id": entry.get("id", ""),
            "type": block_type.key,
            "variant": entry.get("variant", ""),
            "version": entry.get("version", ""),
            "name": block_type.name,
            "fields": {spec.key: spec for spec in block_type.fields},
        },
        "f": fields,
        "v": view,
        "S": ctx.S if ctx is not None else {},
        "editing": editing,
        "pad": "br-pad",
    }
    return block_type, view, context


def block_views(utskick, ctx, data=None, *, record=None):
    """[(entry, BlockType, vy)] för blocken som ritas (textversionen)."""
    data = ctx.snapshot if data is None else data
    out = []
    for entry in data.get("blocks") or []:
        built = _block_context(entry, ctx, data, ctx.editing, record)
        if built is None:
            continue
        block_type, view, _context = built
        if view.get("hidden") and not ctx.editing:
            continue
        out.append((entry, block_type, view))
    return out


def _render_entry(entry, ctx, data):
    built = _block_context(entry, ctx, data, ctx.editing)
    if built is None:
        return ""
    block_type, view, context = built
    if view.get("hidden") and not ctx.editing:
        return ""
    return render_to_string(BLOCK_TEMPLATE.format(type=block_type.key), context)


# ---------------------------------------------------------------------------
# Hela mejlet
# ---------------------------------------------------------------------------


def _value_hash(ctx):
    from .. import keys

    if not ctx.address:
        return ""
    return keys.value_hash("email", ctx.address)


def footer_links(utskick, ctx):
    """Sidfotens länkar som [(text, adress)] i ordning (F.1 element 24).
    Utan en mottagare eller kontakt (redigeraren), och i ett testmejl,
    pekar Ändra och Avregistrera på länkvärden."""
    from .. import links

    data = ctx.snapshot or {}
    company = data.get("company") or {}
    base = links.email_link_base() + "/"
    value_hash = _value_hash(ctx)
    account_id = getattr(ctx.account, "pk", None)
    # Ett testmejl visar en kontakts värden men går till kunden själv:
    # länkarna får aldrig avregistrera kontakten.
    if value_hash and account_id and not ctx.test:
        preferences = links.email_preferences_url(account_id, value_hash)
        unsubscribe = links.unsubscribe_url(account_id, value_hash)
    else:
        preferences = unsubscribe = base
    out = [(PREFERENCES_TEXT, preferences), (UNSUBSCRIBE_TEXT, unsubscribe)]
    if not ctx.web:
        out.append((WEB_VIEW_TEXT, web_url(utskick, ctx)))
    privacy = company.get("privacy_url") or ""
    if privacy:
        name = company.get("display_name") or company.get("name") or ""
        out.append((f"Så hanterar {name} dina uppgifter", privacy))
    return out


def web_url(utskick, ctx):
    from .. import links

    if not getattr(utskick, "pk", None):
        return links.email_link_base() + "/"
    recipient_id = getattr(ctx.recipient, "pk", None)
    return links.web_view_url(utskick.pk, recipient_id)


def company_lines(ctx):
    """(namn, rader): "Exempelrör AB" och ["Mossvägen 12", "167 33 Bromma",
    "08-123 456 78"]. En uppgift per rad (Giovanni 2026-10-10): adressen
    delas vid kommatecken och radbrytningar, telefonen står sist."""
    company = (ctx.snapshot or {}).get("company") or {}
    address = str(company.get("address") or "")
    lines = [part.strip() for part in re.split(r"[,\n]", address) if part.strip()]
    phone = _phone(company.get("phone")) or company.get("phone_text") or ""
    if phone:
        lines.append(phone)
    return company.get("name") or "", lines


def pixel_url(ctx):
    """Öppningspixeln (H.5): bara i send, när utskicket spårar öppningar och
    mottagaren sagt ja till en text som nämner det (tracking_ok)."""
    from .. import links

    recipient = ctx.recipient
    if ctx.mode != SEND or ctx.web or recipient is None or not getattr(recipient, "pk", None):
        return ""
    if not (getattr(ctx.utskick, "open_tracking", False) and ctx.tracking_ok):
        return ""
    return links.pixel_url(recipient.pk)


def _header_html(utskick, ctx):
    data = ctx.snapshot or {}
    logo = data.get("logo") if data.get("logo_position") != "none" else None
    position = data.get("logo_position") if logo else "none"
    show_pre = not ctx.web
    if not (logo or show_pre):
        return ""
    pre_align = {"left": "right", "center": "center"}.get(position, "left")
    return render_to_string(
        HEADER_TEMPLATE,
        {
            "S": ctx.S,
            "logo": logo,
            "center": position == "center",
            "pre_align": pre_align,
            "show_pre": show_pre,
            "web_url": web_url(utskick, ctx),
            "web_text": WEB_VIEW_TEXT,
            "editing": ctx.editing,
        },
    )


def _footer_html(utskick, ctx):
    name, lines = company_lines(ctx)
    return render_to_string(
        FOOTER_TEMPLATE,
        {
            "S": ctx.S,
            "name": name,
            "lines": lines,
            "links": footer_links(utskick, ctx),
            "pixel": pixel_url(ctx),
            "editing": ctx.editing,
        },
    )


def subject_for(utskick, ctx):
    """Ämnesraden sammanfogad, på en rad (F.3)."""
    text = ctx.m(utskick.subject) if ctx is not None else str(utskick.subject or "")
    return " ".join(text.split())


def preheader_for(utskick, ctx):
    """Förhandstexten sammanfogad, på en rad (F.3)."""
    text = ctx.m(utskick.preheader) if ctx is not None else str(utskick.preheader or "")
    return " ".join(text.split())


def render_html(utskick, ctx, mode=None):
    """Hela mejlet som ett HTML-dokument (se modulens beskrivning)."""
    if mode is not None and mode != ctx.mode:
        ctx.mode = mode
    data = ctx.snapshot or {}
    blocks_html = "".join(_render_entry(entry, ctx, data) for entry in data.get("blocks") or [])
    preheader = preheader_for(utskick, ctx)
    css = style.mobile_css()
    if ctx.editing:
        # !important: rabattkodens tomma span har display i sin inline-stil.
        css += "body[data-brev-editing] [data-pb-empty]{display:none !important}"
    html = render_to_string(
        LAYOUT_TEMPLATE,
        {
            "S": ctx.S,
            "css": mark_safe(css),
            "title": subject_for(utskick, ctx) or utskick.name,
            "preheader": preheader,
            "preheader_pad": mark_safe(PREHEADER_PAD) if preheader else "",
            "header_html": mark_safe(_header_html(utskick, ctx)),
            "blocks_html": mark_safe(blocks_html),
            "footer_html": mark_safe(_footer_html(utskick, ctx)),
            "editing": ctx.editing,
            "width": style.WIDTH,
        },
    )
    if ctx.editing:
        # Först i <head>, före allt annat: inget i mejlet körs i redigeraren.
        html = html.replace("<head>", f"<head>\n{EDITING_CSP}", 1)
    return html


def render_block(utskick, block, *, ctx=None):
    """Ett block (<tr>) i läget editor, med {% pb %}-attributen, för
    redigeraren (rita om ett block efter en ändring). block är blocket som
    det står i email_doc (med versioner)."""
    ctx = ctx or context_for(utskick, mode=EDITOR)
    if ctx.mode != EDITOR:
        ctx.mode = EDITOR
    block_type = EMAIL_TYPES.get(block.get("type")) if isinstance(block, dict) else None
    if block_type is None:
        return mark_safe("")
    version = blocks.active_version(block) or {}
    entry = {
        "id": str(block.get("id") or ""),
        "type": block_type.key,
        "variant": block.get("variant") or "",
        "version": version.get("id", ""),
        "fields": blocks.active_fields(block),
    }
    data = dict(ctx.snapshot or {})
    if entry["type"] == "reviews":
        review = _reviews_data(ctx.account, entry["fields"])
        data["reviews"] = {**(data.get("reviews") or {}), entry["id"]: review}
    missing = _media_needs([entry])
    images_now = dict(data.get("images") or {})
    if any(f"{a}:{p}" not in images_now for a, p in missing):
        from apps.flamingo.models import MediaAsset

        assets = {
            a.pk: a
            for a in MediaAsset.objects.filter(account=ctx.account, pk__in={a for a, _p in missing})
        }
        for asset_id, purpose in missing:
            asset = assets.get(asset_id)
            info = _image_info(asset, purpose, False) if asset is not None else None
            if info is not None:
                images_now[f"{asset_id}:{purpose}"] = info
        data["images"] = images_now
    return mark_safe(_render_entry(entry, ctx, data))


# ---------------------------------------------------------------------------
# Länkarna, storleken, webbversionen och kalendern
# ---------------------------------------------------------------------------


def _collect(entries, data, utskick=None):
    ctx = None
    if utskick is not None:
        ctx = RenderContext(
            utskick=utskick,
            account=utskick.account,
            mode=PREVIEW,
            snapshot=data,
            palette=style.palette_for_accent(data.get("accent")),
            S=style.brev_styles(style.palette_for_accent(data.get("accent"))),
        )
    record = []
    for entry in entries:
        _block_context(entry, ctx, data, False, record)
    return record


def collect_links(utskick, doc=None, *, data=None):
    """De spårade länkarna i mejlet som frysningen gör till TrackedLink:
    [LinkSpot(block_id, plats, adress, etikett)] i renderingens ordning.
    Ett block som inte ritas (en video utan bild) har inga. data: det
    frysta underlaget (snapshot()), så att länkarna räknas på exakt det
    som skickas; utan det byggs underlaget nu."""
    if not (isinstance(data, dict) and isinstance(data.get("blocks"), list)):
        data = build_data(utskick, create=False, doc=doc)
    return _collect(data["blocks"], data, utskick)


def spots_for_blocks(raw_blocks):
    """Länkarna i en blocklista utan utskick (blocks.urls): som
    collect_links, men utan Företagets uppgifter (kartlänken saknas)."""
    entries = []
    for block in raw_blocks or []:
        if not isinstance(block, dict) or block.get("type") not in EMAIL_TYPES:
            continue
        entries.append(
            {
                "id": str(block.get("id") or ""),
                "type": block.get("type"),
                "fields": blocks.active_fields(block),
            }
        )
    record = []
    for entry in entries:
        block_type = EMAIL_TYPES[entry["type"]]
        fields = {s.key: entry["fields"].get(s.key, s.empty()) for s in block_type.fields}
        # Utan bilder ritas bild- och videoblockens länkar inte; adresserna
        # i fälten räknas ändå (för kontrollerna av värdarna).
        _prepare(
            block_type.key,
            fields,
            _with_dummy_images(fields, block_type),
            None,
            Linker(None, entry["id"], record),
            False,
        )
    return record


def _with_dummy_images(fields, block_type):
    """Ett underlag där varje bild i blocket finns, så att länkarna i bild-
    och videoblocken räknas utan att filerna läses."""
    images = {}
    for spec in block_type.fields:
        if spec.kind == MEDIA and isinstance(fields.get(spec.key), int):
            purpose = PURPOSES.get((block_type.key, spec.key), "content")
            images[f"{fields[spec.key]}:{purpose}"] = {"url": "https://x", "width": 1, "height": 1}
    return {"images": images, "reviews": {}}


#: Längsta tänkbara värden i storlekskontrollen (F.5).
_LONG_ID = 10**12 - 1


def html_size(utskick):
    """Mejlets storlek i byte (UTF-8) med de längsta värdena: varje
    platshållare 60 tecken, personliga länkar för en mottagare med ett
    tolvsiffrigt id, en spårad länk per plats och pixeln. Gmail kapar mejl
    över ungefär 102 kB (F.5)."""
    from .. import composer

    data = build_data(utskick, create=False)
    spots = _collect(data["blocks"], data, utskick)
    data["links"] = {f"{s.block_id}:{s.position}": _LONG_ID for s in spots}
    values = {name: "W" * composer.VALUE_MAX for name in composer.TAG_NAMES}
    for key in blocks.account_field_keys(utskick.account):
        values[composer.FIELD_PREFIX + key] = "W" * composer.VALUE_MAX
    recipient = SimpleNamespace(
        pk=_LONG_ID,
        merge=values,
        basis="consent",
        tracking_ok=True,
        address=("w" * 64) + "@" + ("w" * 60) + ".example",
    )
    ctx = context_for(utskick, mode=SEND, recipient=recipient, snapshot=data)
    real_open = utskick.open_tracking
    try:
        utskick.open_tracking = True
        html = render_html(utskick, ctx)
    finally:
        utskick.open_tracking = real_open
    return len(html.encode("utf-8"))


def web_view(utskick, recipient=None):
    """Webbversionen (/w/): mottagarens mejl som det frystes, utan pixeln och
    utan "Visa i webbläsaren". Före frysningen (ett testmejl) ritas mejlet
    som det ser ut nu."""
    snap = utskick.email_snapshot if isinstance(utskick.email_snapshot, dict) else {}
    frozen = isinstance(snap.get("blocks"), list)
    ctx = context_for(
        utskick,
        mode=SEND if frozen else PREVIEW,
        recipient=recipient,
        snapshot=snap if frozen else None,
    )
    ctx.web = True
    return render_html(utskick, ctx)


def _ics_escape(text):
    text = str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def _ics_fold(line):
    """Rader över 75 oktetter viks (RFC 5545 3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for char in line:
        encoded = char.encode("utf-8")
        limit = 75 if not parts else 74
        if len(current) + len(encoded) > limit:
            parts.append(current.decode("utf-8"))
            current = b""
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def calendar_ics(utskick, block_id, *, now=None):
    """Händelseblocket som en iCalendar-fil, eller None när blocket saknas,
    inte har ett datum eller inte erbjuder kalendern. Det frysta mejlet
    gäller när det finns."""
    from datetime import UTC

    snap = utskick.email_snapshot if isinstance(utskick.email_snapshot, dict) else {}
    if isinstance(snap.get("blocks"), list):
        entries, company = snap["blocks"], snap.get("company") or {}
    else:
        entries = [
            {"id": str(b.get("id") or ""), "type": b.get("type"), "fields": fields}
            for b, _t, fields in blocks.active_blocks(utskick)
        ]
        company = {"name": _company(utskick.account).get("name")}
    entry = next((e for e in entries if e.get("id") == block_id and e.get("type") == "event"), None)
    if entry is None:
        return None
    f = entry.get("fields") or {}
    day = parse_date(f.get("date"))
    if day is None or f.get("calendar") == "no":
        return None
    from apps.sms.pricing import STOCKHOLM

    stamp = (now or timezone.now()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ADX Flamingo//Utskick//SV",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{utskick.pk}-{block_id}@klick.adx.se",
        f"DTSTAMP:{stamp}",
    ]
    start = f.get("start")
    if start and re.fullmatch(r"\d{2}:\d{2}", start):
        begin = datetime.combine(day, time.fromisoformat(start)).replace(tzinfo=STOCKHOLM)
        end_text = f.get("end")
        end = None
        if end_text and re.fullmatch(r"\d{2}:\d{2}", end_text):
            end = datetime.combine(day, time.fromisoformat(end_text)).replace(tzinfo=STOCKHOLM)
        if end is None or end <= begin:
            end = begin + timedelta(hours=1)
        lines.append("DTSTART:" + begin.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
        lines.append("DTEND:" + end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
    else:
        lines.append("DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"))
        lines.append("DTEND;VALUE=DATE:" + (day + timedelta(days=1)).strftime("%Y%m%d"))
    title = f.get("title") or company.get("name") or "Händelse"
    lines.append("SUMMARY:" + _ics_escape(title))
    if f.get("place"):
        lines.append("LOCATION:" + _ics_escape(f["place"]))
    if company.get("name"):
        lines.append("DESCRIPTION:" + _ics_escape(company["name"]))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


def size_kb(size):
    """Byte som hela kB uppåt (kontrollernas text)."""
    return max(1, math.ceil(size / 1000))
