"""
Brevs färger och stilar (README F.2, F.4, D9).

En accentfärg per utskick (Utskick.accent). Ljus färg: svart knapptext och
mörkare länkar. palette_for_accent återanvänder mix, luminance, contrast
och ensure_contrast ur apps/flamingo/pagebuilder/render.py:

    button_bg   = accent
    button_text = #111111 om contrast(accent, #FFFFFF) < 4.5, annars #FFFFFF
    accent_text = ensure_contrast(accent, #FFFFFF, 4.5)   länkar, siffror,
                  citatlinjer och datumrutan

    ACCENT_RE                     ^#[0-9A-Fa-f]{6}$
    DEFAULT_ACCENT = "#1A57D6"    Exempelrörs blå; demo och tester
    SWATCHES                      mockupens fem: Blå, Grön, Röd, Lila, Svart
    LIGHT_TEXT                    varningen under väljaren
    valid_accent(value) -> str    "#1A57D6" eller "" (ogiltig)
    default_accent(account) -> str
                                  logo_colors_for_account(account.pk)["primary"],
                                  annars DEFAULT_ACCENT
    accent_for(utskick) -> str    utskickets färg, annars kontots standard
    picker(account) -> dict       loggans upp till tre färger, sedan SWATCHES (F.2)
    palette_for_accent(value) -> Palette   (accent, button_bg, button_text, accent_text, light)
    brev_styles(palette) -> dict  S: varje inline-stil som mallarna använder
                                  (style="{{ S.h1 }}"); tokens ur mockupens .t-brev

Tokens från .t-brev (F.4; test_s3_render jämför mot mockupen): ink #202124,
text #3C4043, muted #5F6368, line #E3E6EB, soft #F5F7FA, star #E8A400,
footer ink #5F6368, inre radie 6, knappens radie 6, h1 28/700, h2 24/700,
h3 17/700, ingress 18, brödtext 16/1.65, knapp 16/600 med 15 x 24 i
utfyllnad. Bredd 560, sidmarginal 28 (20 under 620 px), 30 under varje
block, sidhuvudet 28 överst. Typsnitt: -apple-system, "Segoe UI", Roboto,
Helvetica, Arial, sans-serif (i style-attributen med enkla citattecken, så
att attributet inte bryts).

Värdena i S är bara konstanter och färger som prövats mot ACCENT_RE: inget
som kunden skrivit hamnar i en stil.
"""

import re
from dataclasses import dataclass

from apps.flamingo.pagebuilder.render import contrast, ensure_contrast

ACCENT_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
DEFAULT_ACCENT = "#1A57D6"
#: Mockupens färger efter loggans (F.2): (namn, färg).
SWATCHES = (
    ("Blå", "#1A57D6"),
    ("Grön", "#1F7A4D"),
    ("Röd", "#B42318"),
    ("Lila", "#6D28D9"),
    ("Svart", "#111111"),
)
LIGHT_TEXT = "Ljus färg: knapptexten blir svart och länkarna får en mörkare nyans."

WHITE = "#FFFFFF"
DARK_TEXT = "#111111"
#: Kontrastkravet (WCAG AA, normal text).
AA = 4.5
#: Under den här kontrasten mot vitt syns en fylld knapp inte mot kortet
#: (#FAFAFA, ljust grått, pastell): knappen får en kant i den mörkare
#: länkfärgen. Mockupens gula (#FFD400, 1,4) är tydlig nog och ritas som i
#: mockupen.
FAINT = 1.25
#: Högst så många färger ur loggan i väljaren (F.2).
LOGO_SWATCHES = 3

# Tokens ur .t-brev i mockups/flamingo-epost-brev.html (F.4).
TOKENS = {
    "outer": "#FFFFFF",
    "card": "#FFFFFF",
    "hdr-bg": "#FFFFFF",
    "ftr-bg": "#FFFFFF",
    "hdr-ink": "#202124",
    "hdr-muted": "#80868B",
    "ftr-ink": "#5F6368",
    "ftr-muted": "#9AA0A6",
    "ink": "#202124",
    "text": "#3C4043",
    "muted": "#5F6368",
    "line": "#E3E6EB",
    "soft": "#F5F7FA",
    "star": "#E8A400",
    "inner-radius": "6px",
    "btn-radius": "6px",
    "pad": "28px",
    "width": "560px",
    "h1": "28px",
    "h1w": "700",
}
INK = TOKENS["ink"]
TEXT = TOKENS["text"]
MUTED = TOKENS["muted"]
LINE = TOKENS["line"]
SOFT = TOKENS["soft"]
STAR = TOKENS["star"]
HDR_MUTED = TOKENS["hdr-muted"]
FTR_INK = TOKENS["ftr-ink"]
FTR_MUTED = TOKENS["ftr-muted"]
#: Mockupens platshållare för en bild som saknas (bara i redigeraren).
IMG_BG = "#E9EEF7"
IMG_INK = "#5A6B8C"
OFFER_BORDER = "#B9C6DD"

#: Geometrin (F.4).
WIDTH = 560
PAD = 28
PAD_MOBILE = 20
GAP = 30
HEADER_TOP = 28
#: Innehållets bredd: 560 minus sidmarginalerna.
CONTENT = WIDTH - 2 * PAD
MOBILE_BREAK = 620
INNER_RADIUS = 6
BUTTON_RADIUS = 6
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,Menlo,'Courier New',monospace"
#: Underskriftens skrivstil (F.1 element 21): bara systemets typsnitt.
SCRIPT = "'Snell Roundhand','Segoe Script','Bradley Hand',cursive"


def valid_accent(value):
    """Färgen med stora bokstäver om den är giltig, annars ""."""
    text = str(value or "").strip()
    return text.upper() if ACCENT_RE.match(text) else ""


def default_accent(account):
    """Kontots standardfärg: loggans huvudfärg (media.logo_colors_for_account),
    annars DEFAULT_ACCENT. Demot och testerna har aldrig en riktig kunds
    färger (F.2)."""
    from apps.flamingo import media

    try:
        colors = media.logo_colors_for_account(account.pk) if account is not None else {}
    except Exception:  # noqa: BLE001 - färgen är ett förslag, aldrig ett fel
        colors = {}
    return valid_accent((colors or {}).get("primary")) or DEFAULT_ACCENT


def accent_for(utskick):
    """Utskickets färg (Utskick.accent), annars kontots standard."""
    return valid_accent(getattr(utskick, "accent", "")) or default_accent(
        getattr(utskick, "account", None)
    )


@dataclass(frozen=True)
class Palette:
    accent: str
    button_bg: str
    button_text: str
    accent_text: str
    #: Färgen klarar inte vit text: knapptexten blir svart och länkarna mörkare.
    light: bool
    #: Färgen är nästan vit: den fyllda knappen får en kant (FAINT).
    faint: bool = False

    def as_dict(self):
        return {
            "accent": self.accent,
            "button_bg": self.button_bg,
            "button_text": self.button_text,
            "accent_text": self.accent_text,
            "light": self.light,
            "faint": self.faint,
        }


def is_light(value):
    """Klarar färgen inte vit text (kontrast under 4,5 mot vitt)?"""
    hex_value = valid_accent(value) or DEFAULT_ACCENT
    return contrast(hex_value, WHITE) < AA


def palette_for_accent(value):
    """Paletten för en accentfärg (F.2). En ogiltig färg ger standarden."""
    accent = valid_accent(value) or DEFAULT_ACCENT
    light = contrast(accent, WHITE) < AA
    return Palette(
        accent=accent,
        button_bg=accent,
        button_text=DARK_TEXT if light else WHITE,
        accent_text=ensure_contrast(accent, WHITE, AA).upper(),
        light=light,
        faint=contrast(accent, WHITE) < FAINT,
    )


def picker(account):
    """Väljarens färger (F.2): upp till tre ur loggan, sedan mockupens fem.
    {"logo": [{"name", "value", "light"}], "swatches": [...], "default",
    "light_text"}. En färg står bara en gång."""
    from apps.flamingo import media

    try:
        colors = media.logo_colors_for_account(account.pk) or {}
    except Exception:  # noqa: BLE001 - väljaren fungerar utan loggans färger
        colors = {}
    seen = set()
    logo = []
    for n, raw in enumerate(colors.get("colors") or [], start=1):
        value = valid_accent(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        logo.append({"name": f"Loggans färg {n}", "value": value, "light": is_light(value)})
        if len(logo) == LOGO_SWATCHES:
            break
    swatches = [
        {"name": name, "value": value, "light": is_light(value)}
        for name, value in SWATCHES
        if value not in seen
    ]
    return {
        "logo": logo,
        "swatches": swatches,
        "default": default_accent(account),
        "light_text": LIGHT_TEXT,
    }


def _font(weight, size, line):
    return f"font-family:{FONT};font-size:{size}px;line-height:{line};font-weight:{weight};"


def brev_styles(palette):
    """S: varje inline-stil som Brev-mallarna använder, med paletten ifylld.
    Nycklarna är mallarnas namn (style="{{ S.h1 }}")."""
    p = palette if isinstance(palette, Palette) else palette_for_accent(palette)
    acc = p.accent_text
    radius = f"{INNER_RADIUS}px"
    btn_radius = f"{BUTTON_RADIUS}px"
    reset = "margin:0;padding:0;"
    return {
        # Ramarna
        "body": f"margin:0;padding:0;background-color:{WHITE};",
        "outer": f"background-color:{TOKENS['outer']};",
        "outer_td": f"padding:28px 14px;background-color:{TOKENS['outer']};",
        "main": f"width:100%;max-width:{WIDTH}px;background-color:{TOKENS['card']};",
        "blk": (
            f"padding:0 {PAD}px {GAP}px;background-color:{TOKENS['card']};"
            f"font-family:{FONT};color:{TEXT};vertical-align:top;"
        ),
        "hdr": (
            f"padding:{HEADER_TOP}px {PAD}px {GAP}px;background-color:{TOKENS['hdr-bg']};"
            f"font-family:{FONT};color:{TEXT};"
        ),
        "ftr": (
            f"padding:22px {PAD}px 30px;background-color:{TOKENS['ftr-bg']};"
            f"border-top:1px solid {LINE};font-family:{FONT};color:{FTR_MUTED};"
        ),
        "preheader": (
            "display:none;max-height:0;max-width:0;overflow:hidden;mso-hide:all;"
            "font-size:1px;line-height:1px;color:#FFFFFF;opacity:0;"
        ),
        # Sidhuvudet
        "pre": f"{reset}margin-bottom:16px;" + _font(400, 12, 1.4),
        "pre_link": f"color:{HDR_MUTED};text-decoration:underline;",
        # Loggans mått sätter _header.html efter (width:Wpx;height:Hpx; som
        # attributen, render._logo_info): en fast höjd med max-width klämde
        # ihop en bred logga.
        "logo": "display:block;border:0;outline:none;",
        "logo_center": "display:block;border:0;outline:none;margin:0 auto;",
        "logo_text": _font(800, 20, 1) + f"color:{TOKENS['hdr-ink']};letter-spacing:-.01em;",
        # Text
        "kick": (
            f"{reset}margin-bottom:12px;"
            + _font(700, 12, 1)
            + f"color:{MUTED};letter-spacing:.06em;text-transform:uppercase;"
        ),
        "h1": f"{reset}" + _font(700, 28, 1.12) + f"color:{INK};letter-spacing:-.02em;",
        "h2": f"{reset}" + _font(700, 24, 1.2) + f"color:{INK};letter-spacing:-.01em;",
        "h3": f"{reset}" + _font(700, 17, 1.3) + f"color:{INK};",
        "p": f"{reset}" + _font(400, 16, 1.65) + f"color:{TEXT};",
        "lead": f"{reset}margin-top:12px;" + _font(400, 18, 1.6) + f"color:{TEXT};",
        "sm": f"{reset}" + _font(400, 14, 1.5) + f"color:{MUTED};",
        "ul": "margin:12px 0 0;padding:0 0 0 20px;" + _font(400, 16, 1.65) + f"color:{TEXT};",
        "li": "margin:0;padding:0;" + _font(400, 16, 1.65) + f"color:{TEXT};",
        "link": f"color:{acc};text-decoration:underline;",
        "link_sm": f"color:{acc};text-decoration:underline;" + _font(400, 14, 1.5),
        "strong_ink": f"color:{INK};font-weight:600;",
        # Knappar (tabellceller med bgcolor och utfyllnad, ingen VML)
        "btn_td": (
            f"border-radius:{btn_radius};background-color:{p.button_bg};mso-padding-alt:15px 24px;"
            + (f"border:1px solid {acc};" if p.faint else "")
        ),
        "btn_a": (
            "display:inline-block;padding:15px 24px;"
            + _font(600, 16, 1)
            + f"color:{p.button_text};text-decoration:none;border-radius:{btn_radius};"
        ),
        "btn2_td": (
            f"border-radius:{btn_radius};border:1px solid {acc};mso-padding-alt:14px 23px;"
        ),
        "btn2_a": (
            "display:inline-block;padding:14px 23px;"
            + _font(600, 16, 1)
            + f"color:{acc};text-decoration:none;border-radius:{btn_radius};"
        ),
        "btn_wrap": "display:inline-block;vertical-align:top;margin:3px 4px 3px 0;",
        "btn_bg": p.button_bg,
        "accent_text": acc,
        # Bilder
        "img": (
            f"display:block;width:100%;max-width:{CONTENT}px;height:auto;border:0;outline:none;"
            f"border-radius:{radius};"
        ),
        "img_ph": (
            f"background-color:{IMG_BG};color:{IMG_INK};border-radius:{radius};text-align:center;"
            "vertical-align:middle;" + _font(500, 12, 1.4)
        ),
        "caption": f"{reset}margin-top:8px;" + _font(400, 14, 1.5) + f"color:{MUTED};",
        # Kolumner och rutor
        "col": "display:inline-block;width:100%;vertical-align:top;",
        "tile": f"border-top:1px solid {LINE};padding:14px 0 0;",
        "tile_n": f"{reset}margin-bottom:10px;" + _font(700, 13, 1) + f"color:{acc};",
        "line": f"border-top:1px solid {LINE};font-size:0;line-height:0;height:1px;",
        "offer": (
            f"background-color:{SOFT};border-radius:{radius};border:1px dashed {OFFER_BORDER};"
            "padding:26px 20px;text-align:left;"
        ),
        "offer_kick": (
            f"{reset}margin-bottom:8px;"
            + _font(700, 12, 1)
            + f"color:{MUTED};letter-spacing:.06em;text-transform:uppercase;"
        ),
        "offer_h": f"{reset}" + _font(700, 22, 1.2) + f"color:{INK};letter-spacing:-.01em;",
        "offer_sm": f"{reset}margin:6px 0 16px;" + _font(400, 14, 1.5) + f"color:{MUTED};",
        "code": (
            f"display:inline-block;font-family:{MONO};font-size:22px;line-height:1;"
            f"font-weight:700;letter-spacing:.14em;color:{INK};background-color:{WHITE};"
            f"border:1px solid {LINE};border-radius:{BUTTON_RADIUS + 2}px;padding:11px 18px;"
        ),
        "price_td": (
            f"padding:11px 0;border-bottom:1px solid {LINE};"
            + _font(400, 16, 1.3)
            + f"color:{TEXT};"
        ),
        "price_v": (
            f"padding:11px 0 11px 12px;border-bottom:1px solid {LINE};"
            + _font(700, 16, 1.3)
            + f"color:{INK};text-align:right;white-space:nowrap;"
        ),
        "price_td_last": "padding:11px 0;" + _font(400, 16, 1.3) + f"color:{TEXT};",
        "price_v_last": (
            "padding:11px 0 11px 12px;"
            + _font(700, 16, 1.3)
            + f"color:{INK};text-align:right;white-space:nowrap;"
        ),
        "hours_v": (
            f"padding:11px 0 11px 12px;border-bottom:1px solid {LINE};"
            + _font(400, 16, 1.3)
            + f"color:{INK};text-align:right;white-space:nowrap;"
        ),
        "hours_v_last": (
            "padding:11px 0 11px 12px;"
            + _font(400, 16, 1.3)
            + f"color:{INK};text-align:right;white-space:nowrap;"
        ),
        "stars": f"color:{STAR};letter-spacing:2px;font-size:16px;",
        "quote": f"border-left:3px solid {acc};padding-left:16px;",
        "num": (
            f"width:28px;height:28px;border-radius:50%;border:1px solid {acc};"
            f"background-color:{WHITE};color:{acc};text-align:center;vertical-align:middle;"
            + _font(700, 14, 1)
        ),
        "date": (
            f"width:66px;border:1px solid {acc};border-radius:{radius};text-align:center;"
            f"padding:9px 0;background-color:{WHITE};"
        ),
        "date_mo": f"{reset}" + _font(700, 11, 1) + f"color:{acc};letter-spacing:.1em;",
        "date_dy": f"{reset}margin-top:3px;" + _font(700, 26, 1.1) + f"color:{INK};",
        "av": (
            f"width:56px;height:56px;border-radius:50%;background-color:{SOFT};color:{acc};"
            "text-align:center;vertical-align:middle;" + _font(700, 17, 1)
        ),
        "av_sm": (
            f"width:48px;height:48px;border-radius:50%;background-color:{SOFT};color:{acc};"
            "text-align:center;vertical-align:middle;" + _font(700, 15, 1)
        ),
        "av_img": "display:block;width:56px;height:56px;border:0;border-radius:50%;",
        "av_img_sm": "display:block;width:48px;height:48px;border:0;border-radius:50%;",
        "callout": f"background-color:{SOFT};border-radius:{radius};padding:16px 18px;",
        "sig": f"{reset}margin:6px 0 12px;font-family:{SCRIPT};font-size:34px;line-height:1;"
        f"font-weight:600;color:{INK};",
        "soc": f"color:{acc};text-decoration:underline;" + _font(400, 15, 1.5),
        "soc_gap": "display:inline-block;width:14px;",
        # Sidfoten
        "ftr_p": f"{reset}" + _font(400, 14, 1.5) + f"color:{FTR_MUTED};",
        "ftr_b": f"color:{FTR_INK};font-weight:700;",
        "ftr_a": f"color:{FTR_MUTED};text-decoration:underline;",
    }


def mobile_css():
    """Mediefrågan som bara förfinar (F.4): sidmarginalen 20 px och
    kolumnerna i full bredd under 620 px. Utan den (Gmail-appen) håller
    layouten ändå: kolumnerna är inline-block med max-width."""
    return (
        ":root{color-scheme:light;supported-color-schemes:light}"
        f"@media (max-width:{MOBILE_BREAK}px){{"
        ".br-main{width:100%!important}"
        f".br-pad{{padding-left:{PAD_MOBILE}px!important;padding-right:{PAD_MOBILE}px!important}}"
        ".br-col{display:block!important;width:100%!important;max-width:100%!important}"
        ".br-colpad{padding:0 0 14px 0!important}"
        ".br-h1{font-size:28px!important}"
        "}"
    )
