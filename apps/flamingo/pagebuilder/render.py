"""
Renderaren för designen Ren (templates/flamingo/lp/ren/,
static/css/flamingo-lp-ren.css).

    render_block_html(page, block, account, *, editing=False, request=None) -> SafeString
    render_page_html(page, account, campaign=None, *, editing=False, which="draft",
                     request=None, form=None, extra=None) -> str
    page_view_context(...)  samma kontext som render_page_html, för andra mallar
                            (tacksidan) som ärver ren/layout.html
    palette_vars(page)      palettens CSS-variabler, kontrasten prövad (WCAG AA)

Allt i blocken är vanlig text och escapas av mallarna; ingenting markeras
som säkert utom HTML som mallarna själva ritat.

Redigeringsläget (editing=True) lägger till attribut för redigeraren och
ändrar inte layouten:

    blockets yttersta element  data-pb-block="b_..." data-pb-type="hero"
                               data-pb-variant="call" data-pb-version="v_..."
    en text som går att ändra  data-pb-field="title", i listor och poster med
                               index: "points.0", "steps.1.title", "items.2.a"
    en bild                    data-pb-field="image" data-pb-media
    ett tomt fält              ett tomt element med data-pb-empty och
                               data-pb-placeholder="Ingress" (dolt med CSS,
                               redigeraren visar det)
    ett dolt block             (omdömen utan valda omdömen, före och efter
                               utan bilder) ritas som en tom sektion med
                               data-pb-empty, också dold

Utan editing finns inget av det i HTML:en. Med editing får dokumentet en
Content-Security-Policy med script-src 'none' (EDITING_CSP): redigeraren
ritar det i en ram med srcdoc, och inget skript i sidan får köras där.

Länkarna till Google (profilen, omdömet, författaren) prövas igen här med
reviews.google_link, och betyg och omdömen från en Google-profil som inte
är intygad som kundens visas aldrig (FlamingoAccount.google_profile_trusted).

Blocket "Omdömen från Reco" ritar Recos egen ruta: iframe-adressen byggs här
med reco.frames, bara av siffrorna i kundens id, och bara när profilen är
intygad som kundens (FlamingoAccount.reco_trusted). Länken till profilen
prövas igen med reco.profile_link. Varianterna Utvalda ritar de omdömen
kunden valt (reco.selected_reviews, länkarna byggda av omdömenas id), och
blir Recos egen ruta (Liggande stor) när Utvalda är av (reco.selected_enabled,
läst en gång per rendering). Demot ritar en påhittad ruta och laddar aldrig
något från Reco.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from .. import reco, sms
from ..models import RATING_SOURCES, Fact, LandingPage, MediaAsset
from ..reviews import google_link
from .blocks import FormSpec, active_fields, active_version, form_spec, visible_items
from .facts import facts_for
from .registry import MEDIA, TYPES

logger = logging.getLogger(__name__)

LAYOUT_TEMPLATE = "flamingo/lp/ren/page.html"
THANKS_TEMPLATE = "flamingo/lp/ren/thanks.html"
BLOCK_TEMPLATE = "flamingo/lp/ren/blocks/{type}.html"

#: Uppgifter (Fact.key) för ett betyg från Google eller ADX (förut
#: public_views.FACT_RATING).
FACT_RATING = ("betyg", "google-betyg", "omdomen", "rating")


# ---------------------------------------------------------------------------
# Färgerna
# ---------------------------------------------------------------------------

WHITE = "#FFFFFF"
INK = "#202124"
#: Kontrastkravet för text och knappar (WCAG AA, normal text).
AA = 4.5

#: Rens paletter: huvudfärgen (knappar, ringremsan, stegens siffror).
#: Övriga färger räknas fram och prövas mot AA i palette_vars. Den röda är
#: inte felfärgen (ERROR), så att en ruta i paletten aldrig ser ut som ett fel.
PALETTES = {
    LandingPage.PALETTE_BLUE: "#1B66D2",
    LandingPage.PALETTE_GREEN: "#137333",
    LandingPage.PALETTE_RED: "#C5221F",
    LandingPage.PALETTE_ORANGE: "#A8510C",
    LandingPage.PALETTE_GRAPHITE: "#3C4043",
}
#: Accenten (överrubriken, länkarna, ikonerna och de ljusa tonerna) när den
#: inte är huvudfärgen: grafit får mässing, så att sektionerna blir varma i
#: stället för grått på grått.
ACCENTS = {
    LandingPage.PALETTE_GRAPHITE: "#8C5A00",
}
#: Formulärets felfärg (--rn-error i flamingo-lp-ren.css).
ERROR = "#B3261E"
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")  # fullmatch
#: Redigeringslägets dokument (srcdoc i redigeraren): inga skript alls.
EDITING_CSP = '<meta http-equiv="Content-Security-Policy" content="script-src \'none\'">'


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def mix(color, other, amount):
    """color blandad med other: amount 0 ger color, 1 ger other."""
    a, b = _rgb(color), _rgb(other)
    return _hex(tuple(x * (1 - amount) + y * amount for x, y in zip(a, b, strict=True)))


def luminance(color):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ensure_contrast(color, background, ratio=AA):
    """Färgen, mörkad i små steg tills kontrasten mot bakgrunden räcker."""
    step = 0
    while contrast(color, background) < ratio and step < 40:
        color = mix(color, "#000000", 0.05)
        step += 1
    return color


def _primary_for(page):
    if page.palette == LandingPage.PALETTE_LOGO:
        colors = page.logo_colors if isinstance(page.logo_colors, dict) else {}
        value = str(colors.get("primary") or "")
        if _HEX.fullmatch(value):
            return value.upper()
        return PALETTES[LandingPage.PALETTE_BLUE]
    return PALETTES.get(page.palette, PALETTES[LandingPage.PALETTE_BLUE])


def palette_vars(page):
    """{CSS-variabel: färg} för sidans palett, kontrasten prövad mot AA.

    Huvudfärgen bär knapparnas text (--rn-on-primary): vit när vitt klarar
    AA. En ljus färg (oftast från en logotyp) där vitt inte klarar det men
    mörk text (INK) gör det står kvar som den är, med mörk text på
    knapparna; texten i färgen på vitt (--rn-primary-ink: länkar,
    överrubriken) mörkas för sig. En mellanton som inte klarar någon av dem
    mörkas tills vit text klarar AA. De ljusa tonerna och textfärgen räknas
    ur accenten (ACCENTS, annars huvudfärgen), och textfärgen prövas också
    mot den ljusa tonen (chips, rutor)."""
    raw = _primary_for(page)
    on_primary = WHITE
    if contrast(raw, WHITE) >= AA:
        primary = raw
    elif contrast(raw, INK) >= AA:
        primary, on_primary = raw, INK
    else:
        primary = ensure_contrast(raw, WHITE)
    accent = ACCENTS.get(page.palette, primary)
    soft = mix(accent, WHITE, 0.9)
    ink = ensure_contrast(ensure_contrast(accent, WHITE), soft)
    if on_primary == INK:
        hover = mix(primary, WHITE, 0.22)
    else:
        hover = mix(primary, "#000000", 0.16)
    return {
        "--rn-primary": primary,
        "--rn-primary-hover": hover,
        "--rn-on-primary": on_primary,
        "--rn-primary-soft": soft,
        "--rn-primary-softer": mix(accent, WHITE, 0.95),
        "--rn-primary-line": mix(accent, WHITE, 0.72),
        "--rn-primary-ink": ink,
    }


def palette_style(page):
    """:root-regeln med palettens variabler (bara hexkoder, se palette_vars)."""
    pairs = "".join(f"{name}:{value};" for name, value in palette_vars(page).items())
    return f":root{{{pairs}}}"


# ---------------------------------------------------------------------------
# Sidans gemensamma uppgifter
# ---------------------------------------------------------------------------


def _decimal_text(value):
    if value is None:
        return ""
    text = f"{Decimal(value):.1f}"
    return text.replace(".", ",")


def _rating_fact(account):
    """Ett bekräftat betyg från Google eller ADX, som det står ("4,8 av 5"),
    annars "". Ett betyg från hemsidan eller kunden visas aldrig, och inte
    ett från en Google-profil som inte är intygad som kundens."""
    sources = RATING_SOURCES
    if not account.google_profile_trusted:
        sources = tuple(s for s in RATING_SOURCES if s != Fact.SOURCE_GOOGLE)
    rows = dict(
        account.facts.filter(confirmed=True, key__in=FACT_RATING, source__in=sources).values_list(
            "key", "value"
        )
    )
    for key in FACT_RATING:
        value = (rows.get(key) or "").strip()
        if value:
            return value
    return ""


@dataclass
class Site:
    """Det sidhuvudet, sidfoten och flera block delar."""

    business: str
    phone: str = ""
    tel: str = ""
    address: str = ""
    hours: str = ""
    #: Betyget från Google-profilen ("4,8") och antalet omdömen, eller
    #: rating_text: ett bekräftat betyg från Google eller ADX som det står.
    rating: str = ""
    rating_value: float = 0.0
    review_count: int | None = None
    rating_text: str = ""
    maps_uri: str = ""
    logo: object = None
    has_form: bool = False
    has_callbar: bool = False
    hero_variant: str = ""
    spec: FormSpec | None = None
    reviews: list = field(default_factory=list)
    #: Kundens id och länk på Reco, bara när profilen är intygad
    #: (reco_trusted) och kontot inte är demot; annars "".
    reco_venue_id: str = ""
    reco_url: str = ""
    #: Demots påhittade ruta ({"rating", "count", "stars"}), annars None.
    reco_demo: dict | None = None
    #: Utvalda: på eller av (reco.selected_enabled), de valda omdömena, och
    #: betyget och antalet från profilen ("4,9"), bara för en intygad profil
    #: när Utvalda är på.
    reco_enabled: bool = True
    reco_reviews: list = field(default_factory=list)
    reco_rating: str = ""
    reco_rating_value: float = 0.0
    reco_count: int | None = None


def site_info(page, account, blocks, media=None):
    facts = facts_for(account)
    phone = ""
    hero_variant = ""
    for block in blocks:
        if block.get("type") == "hero":
            hero_variant = block.get("variant") or ""
            phone = (active_fields(block).get("phone") or "").strip()
            break
    if not phone:
        for block in blocks:
            if block.get("type") == "callbar":
                phone = (active_fields(block).get("phone") or "").strip()
                break
    logo = None
    if media is not None:
        logo = next((a for a in media.values() if a.is_logo), None)
    if logo is None:
        logo = MediaAsset.objects.filter(account=account, is_logo=True).order_by("-pk").first()
    google_rating = account.trusted_google_rating
    rating = _decimal_text(google_rating)
    reco_live = account.reco_trusted and not account.is_demo
    has_reco = any(b.get("type") == "reviews_reco" for b in blocks)
    reco_enabled = reco.selected_enabled() if has_reco else True
    reco_rating = account.reco_rating if reco_live and reco_enabled else None
    return Site(
        business=facts.company,
        phone=phone,
        tel=sms.tel_href(phone) if phone else "",
        address=facts.address,
        hours=facts.hours,
        rating=rating,
        rating_value=float(google_rating or 0),
        review_count=account.google_review_count if google_rating is not None else None,
        rating_text="" if rating else _rating_fact(account),
        maps_uri=google_link(account.google_maps_uri) if account.google_profile_trusted else "",
        logo=logo,
        has_form=any(b.get("type") == "form" for b in blocks),
        has_callbar=any(b.get("type") == "callbar" for b in blocks),
        hero_variant=hero_variant,
        spec=form_spec(blocks),
        reviews=account.selected_google_reviews(),
        reco_venue_id=reco.clean_venue_id(account.reco_venue_id) if reco_live else "",
        reco_url=reco.profile_link(account.reco_url) if reco_live else "",
        reco_demo=_reco_demo(account),
        reco_enabled=reco_enabled,
        reco_reviews=reco.selected_reviews(account, reco_enabled) if has_reco else [],
        reco_rating=_decimal_text(reco_rating),
        reco_rating_value=float(reco_rating or 0),
        reco_count=account.reco_review_count if reco_rating is not None else None,
    )


def _reco_demo(account):
    """Demots ruta i stället för Recos: det påhittade betyget, aldrig en
    iframe eller en länk till Reco."""
    if not (account.is_demo and account.reco_trusted):
        return None
    return {
        "rating": _decimal_text(account.reco_rating) or "4,7",
        "count": account.reco_review_count,
        "stars": _stars(account.reco_rating or 0),
    }


def _media_map(account, blocks):
    ids = set()
    for block in blocks:
        block_type = TYPES.get(block.get("type"))
        if block_type is None:
            continue
        fields = active_fields(block)
        for spec in block_type.fields:
            if spec.kind == MEDIA and isinstance(fields.get(spec.key), int):
                ids.add(fields[spec.key])
    if not ids:
        return {}
    return {a.pk: a for a in MediaAsset.objects.filter(account=account, pk__in=ids)}


#: Block som blir en smal remsa direkt efter Toppen (förtroende i en rad)
#: i stället för en egen sektion.
STRIP_BLOCKS = {
    ("certificates", "badges"),
    ("reviews_google", "line"),
    ("reviews_reco", "liten"),
    ("reviews_reco", "utvalda_rad"),
}


def _drawn_variant(block, reco_enabled=True):
    """Varianten som ritas: en Utvalda-variant blir Recos egen ruta när
    Utvalda är av (reco.effective_variant)."""
    variant = block.get("variant")
    if block.get("type") == "reviews_reco":
        return reco.effective_variant(variant, reco_enabled)
    return variant


def _surfaces(blocks, reco_enabled=True):
    """{block-id: "plain" | "soft" | "strip" | "band"}: Toppen är vit, sedan
    växlar sektionerna mellan vitt och palettens ljusa ton. Märken eller
    betyget i en rad direkt efter Toppen blir en smal remsa ("strip") som
    inte räknas i växlingen. Ett formulär direkt efter Toppen med formulär
    står bredvid det och delar dess vita yta. Ringremsan är ett eget band."""
    out, soft, previous = {}, True, None
    for block in blocks:
        kind = block.get("type")
        if kind == "hero":
            out[block.get("id")] = "plain"
            soft = True
        elif kind == "callbar":
            out[block.get("id")] = "band"
        elif kind == "form" and previous is not None and _split(previous, block):
            out[block.get("id")] = "plain"
        elif (
            previous is not None
            and previous.get("type") == "hero"
            and (kind, _drawn_variant(block, reco_enabled)) in STRIP_BLOCKS
        ):
            out[block.get("id")] = "strip"
        else:
            out[block.get("id")] = "soft" if soft else "plain"
            soft = not soft
        previous = block
    return out


def _split(hero, form):
    return (
        hero.get("type") == "hero" and hero.get("variant") == "form" and form.get("type") == "form"
    )


# ---------------------------------------------------------------------------
# Per block: det mallen behöver utöver fälten
# ---------------------------------------------------------------------------


def _initials(name):
    parts = [p for p in re.split(r"[\s.]+", name or "") if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def _stars(rating):
    """Fem stjärnor som "full", "half" eller "empty"."""
    try:
        value = float(rating)
    except (TypeError, ValueError):
        value = 0.0
    out = []
    for i in range(5):
        if value >= i + 0.75:
            out.append("full")
        elif value >= i + 0.25:
            out.append("half")
        else:
            out.append("empty")
    return out


def _review_view(review):
    rating = review.get("rating") or 0
    try:
        rating = max(1, min(5, int(rating)))
    except (TypeError, ValueError):
        rating = 5
    # Omdömet på Google Maps (Googles villkor: varje omdöme ska gå att öppna
    # där; reviews.py sparar det som "uri"). Båda länkarna prövas igen här:
    # bara https till Google, inga användaruppgifter eller bakåtsnedstreck.
    return {
        "author": str(review.get("author") or ""),
        "author_uri": google_link(review.get("author_uri")),
        "initials": _initials(str(review.get("author") or "")),
        "rating": rating,
        "stars": _stars(rating),
        "text": str(review.get("text") or ""),
        "relative": str(review.get("relative") or ""),
        "uri": google_link(review.get("uri")),
    }


def _reco_review_view(review):
    """Ett valt omdöme från Reco (reco.selected_reviews, redan prövat):
    namnet som Reco visar det, dagen, betyget, texten oförändrad, länken till
    omdömet på Reco (byggd av id:t och prövad igen) och märkningen
    "Omdöme från inbjuden kund"."""
    try:
        day = date.fromisoformat(str(review.get("date") or ""))
    except ValueError:
        day = None
    author = str(review.get("author") or "")
    return {
        "author": author,
        "initials": _initials(author),
        "rating": review.get("rating"),
        "stars": _stars(review.get("rating")),
        "text": str(review.get("text") or ""),
        "date": day,
        "uri": reco.safe_review_link(reco.review_link(review.get("id"))),
        "invited": review.get("invited") is True,
    }


def _num(value):
    """Ett tal för SVG: punkt som decimaltecken (mallarna skulle skriva
    "320,0" med svensk lokal)."""
    return f"{value:.1f}"


def _area_map(places):
    """Kartan som en egen SVG utan kartbilder: kundens orter runt en mitt,
    med ringar för avståndet. Ingen riktig geografi, och inget hämtas.
    Talen är strängar med punkt (_num)."""
    places = [p for p in places if p][:8]
    width, height = 640, 400
    cx, cy = width / 2, height / 2
    points = []
    if places:
        first, rest = places[0], places[1:]
        points.append(
            {
                "x": _num(cx),
                "y": _num(cy),
                "ly": _num(cy - 26),
                "name": first,
                "anchor": "middle",
                "main": True,
            }
        )
        for i, name in enumerate(rest):
            angle = -math.pi / 2 + i * (2 * math.pi / max(1, len(rest))) + 0.35
            radius = 128 if i % 2 == 0 else 158
            x = cx + math.cos(angle) * radius * 1.45
            y = cy + math.sin(angle) * radius * 0.92
            anchor = "start" if x > cx + 20 else "end" if x < cx - 20 else "middle"
            points.append(
                {"x": _num(x), "y": _num(y), "name": name, "anchor": anchor, "main": False}
            )
    return {"width": width, "height": height, "cx": _num(cx), "cy": _num(cy), "points": points}


def _prepare(block, fields, site, media):
    """Blockets egen kontext utöver fälten."""
    kind = block.get("type")
    view = {}
    if kind in ("hero", "person"):
        view["image"] = media.get(fields.get("image"))
    elif kind == "before_after":
        view["before"] = media.get(fields.get("before"))
        view["after"] = media.get(fields.get("after"))
        view["hidden"] = view["before"] is None or view["after"] is None
    elif kind == "steps":
        view["steps"] = visible_items(block, "steps", fields.get("steps") or [])
    elif kind == "faq":
        view["items"] = visible_items(block, "items", fields.get("items") or [])
    elif kind == "reviews_google":
        limit_items = visible_items(block, "reviews", site.reviews)
        view["reviews"] = [_review_view(r) for r in limit_items]
        view["stars"] = _stars(site.rating_value)
        has_line = bool(site.rating)
        view["hidden"] = not site.reviews and not (block.get("variant") == "line" and has_line)
    elif kind == "reviews_reco":
        # Recos ruta, byggd bara av id:t (reco.frames), eller (Utvalda) de
        # omdömen kunden valt. Utan en intygad profil syns blocket inte, och
        # Utvalda av ritar Recos ruta (reco.effective_variant).
        variant = reco.effective_variant(block.get("variant"), site.reco_enabled)
        view["variant"] = variant
        view["profile_url"] = site.reco_url
        view["demo"] = site.reco_demo
        if variant in reco.SELECTED_VARIANTS:
            view["layout"] = "utvalda"
            view["frames"] = []
            items = visible_items(block, "reviews", site.reco_reviews)
            view["reviews"] = [_reco_review_view(r) for r in items]
            view["stars"] = _stars(site.reco_rating_value)
            shows = bool(site.reco_reviews) or (variant == "utvalda_rad" and bool(site.reco_rating))
        else:
            view["layout"] = "widget"
            view["frames"] = reco.frames(variant, site.reco_venue_id)
            shows = bool(view["frames"])
        view["hidden"] = not shows and view["demo"] is None
    elif kind == "area":
        view["map"] = _area_map(fields.get("places") or [])
    elif kind == "form":
        # Bokningens text står under datumfältet (när ingen ruta med rubrik
        # gör det), annars under kortet.
        view["note_under"] = ""
        if block.get("variant") == "booking" and not (
            fields.get("note_title") and fields.get("note")
        ):
            spec = site.spec
            if spec is not None and spec.block_id == block.get("id"):
                date = next((q for q in spec.questions if q["kind"] == "date"), None)
                view["note_under"] = date["field"] if date else ""
    if kind in ("hero", "callbar"):
        view["tel"] = sms.tel_href(fields.get("phone") or "") if fields.get("phone") else ""
    if kind == "hero":
        view["stars"] = _stars(site.rating_value)
        # Huvudhandlingen: med bild och bara text leder knappen till
        # formuläret när det har frågor (offert, boka tid), annars ringer den
        # (samma regel som koll.hero_main).
        spec = site.spec
        view["to_form"] = block.get("variant") in ("image", "text") and (
            spec is not None and not spec.is_short
        )
        # Långa rubriker och långa sammansatta ord ("varmvattenberedare") får
        # en mindre storlek i stället för att avstavas mitt i rubriken.
        title = str(fields.get("title") or "")
        longest = max((len(w) for w in title.split()), default=0)
        view["long_title"] = len(title) > 34 or longest >= 15
    return view


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@dataclass
class RenderState:
    """Det som är samma för alla block på en sida i en rendering."""

    page: object
    account: object
    blocks: list
    site: Site
    media: dict
    surfaces: dict
    editing: bool = False
    request: object = None
    form: object = None
    extra: dict = field(default_factory=dict)


def _state(page, account, blocks, *, editing=False, request=None, form=None, extra=None):
    media = _media_map(account, blocks)
    site = site_info(page, account, blocks, media)
    return RenderState(
        page=page,
        account=account,
        blocks=blocks,
        site=site,
        media=media,
        surfaces=_surfaces(blocks, site.reco_enabled),
        editing=editing,
        request=request,
        form=form,
        extra=dict(extra or {}),
    )


def _form_context(state):
    """Formulärets kontext: LeadForm (bunden eller tom) och frågorna med
    sina fält, honungsfältet, dolda fält för klick-id och adressen."""
    from ..public_views import HONEYPOT, LeadForm

    spec = state.site.spec or FormSpec()
    form = state.form if state.form is not None else LeadForm(spec=spec)
    questions = []
    for q in spec.questions:
        bound = form[q["field"]] if q["field"] in form.fields else None
        questions.append({**q, "bound": bound})
    return {
        "form": form,
        "questions": questions,
        "spec": spec,
        "honeypot": HONEYPOT,
        "tracking": state.extra.get("tracking") or {},
        "action": state.extra.get("action") or "",
    }


def _block_html(state, block):
    block_type = TYPES.get(block.get("type"))
    if block_type is None:
        return ""
    fields = active_fields(block)
    version = active_version(block) or {}
    view = _prepare(block, fields, state.site, state.media)
    context = {
        "block": block,
        "b": {
            "id": block.get("id", ""),
            "type": block_type.key,
            "variant": block.get("variant"),
            "version": version.get("id", ""),
            "name": block_type.name,
            "fields": {f.key: f for f in block_type.fields},
        },
        "f": fields,
        "view": view,
        "site": state.site,
        "media": state.media,
        "surface": state.surfaces.get(block.get("id"), "plain"),
        "editing": state.editing,
        "preview": state.extra.get("preview", False),
        "page": state.page,
    }
    if block_type.key == "form":
        context.update(_form_context(state))
    if view.get("hidden") and not state.editing:
        return ""
    template = BLOCK_TEMPLATE.format(type=block_type.key)
    return render_to_string(template, context, request=state.request)


def render_block_html(page, block, account, *, editing=False, request=None, blocks=None):
    """Ett block som HTML (för redigeraren: rita om ett block efter en
    ändring). blocks är sidans block som blocket står bland (för ytan och
    sidans telefonnummer); utan dem används utkastet."""
    blocks = page.draft_blocks if blocks is None else blocks
    if not any(b.get("id") == block.get("id") for b in blocks):
        blocks = [*blocks, block]
    else:
        blocks = [block if b.get("id") == block.get("id") else b for b in blocks]
    state = _state(page, account, blocks, editing=editing, request=request)
    # Mallen escapar varje fält; resultatet är HTML som mallen ritat.
    return mark_safe(_block_html(state, block))


def page_view_context(
    page,
    account,
    campaign=None,
    *,
    editing=False,
    which="draft",
    request=None,
    form=None,
    extra=None,
):
    """Kontexten för ren/layout.html och mallarna som ärver den."""
    blocks = page.blocks_for(which)
    state = _state(page, account, blocks, editing=editing, request=request, form=form, extra=extra)
    html = [_block_html(state, block) for block in blocks]
    title = ""
    description = ""
    for block in blocks:
        if block.get("type") == "hero":
            fields = active_fields(block)
            # Fliken och träffen säger tjänsten och orten (överrubriken);
            # rubriken om vad kunden får blir beskrivningen.
            kicker = str(fields.get("kicker") or "").strip()
            title = kicker or fields.get("title") or ""
            description = fields.get("lead") or ""
            if kicker and fields.get("title"):
                description = f"{fields.get('title')} {description}".strip()
            break
    context = {
        "page": page,
        "account": account,
        "campaign": campaign,
        "site": state.site,
        # Varje block är en mall som escapat sina fält.
        "blocks_html": mark_safe("".join(html)),
        "palette_style": palette_style(page),
        "editing": editing,
        "which": which,
        "page_title": title or (campaign.service.name if campaign is not None else page.name),
        "page_description": " ".join(description.split())[:300],
        "has_callbar": state.site.has_callbar,
    }
    context.update(state.extra)
    return context


def render_page_html(
    page,
    account,
    campaign=None,
    *,
    editing=False,
    which="draft",
    request=None,
    form=None,
    extra=None,
):
    """Hela sidan som ett HTML-dokument (doctype till </html>).

    which: "draft" (utkastet) eller "published" (det besökarna ser).
    form: public_views.LeadForm, bunden efter en POST med fel; annars en tom.
    extra: layoutens extra kontext från public_views (preview, is_staff,
    status_label, review_url, call_beacon, tracking, action, draft_url ...).
    request behövs för formulärets CSRF-nyckel."""
    context = page_view_context(
        page,
        account,
        campaign,
        editing=editing,
        which=which,
        request=request,
        form=form,
        extra=extra,
    )
    html = render_to_string(LAYOUT_TEMPLATE, context, request=request)
    if editing:
        # Först i <head>, före varje skript: inget i sidan körs i redigeraren.
        html = html.replace("<head>", f"<head>\n{EDITING_CSP}", 1)
    return html
