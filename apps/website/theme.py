"""
ADX page theme: one hex color per page drives the whole appearance.

Exact Python port of the palette math in strict-design-guide.html
(paletteFromHex + shouldTextBeDark). The client-side shader derives four
colors from the page hex; the ONLY thing the server needs to know is
whether the derived TOP color is light enough for dark text. Computing it
server-side means the correct text color is present in the initial HTML -
no flash while gradient.js boots, and correct rendering without JS at all.

If the derivation rules change in the guide, change them here AND in
static/js/gradient.js - a drift makes the text color flicker at load.
"""

DEFAULT_PAGE_COLOR = "#f7fcff"  # guidens "hem"-färg: ljus isblå


def _hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _rgb_to_hsl(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    lum = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, lum
    d = mx - mn
    s = d / (2 - mx - mn) if lum > 0.5 else d / (mx + mn)
    if mx == r:
        h = ((g - b) / d + (6 if g < b else 0)) / 6
    elif mx == g:
        h = ((b - r) / d + 2) / 6
    else:
        h = ((r - g) / d + 4) / 6
    return h * 360, s, lum


def _hsl_to_rgb(h, s, lum):
    h /= 360
    if s == 0:
        return lum, lum, lum
    q = lum * (1 + s) if lum < 0.5 else lum + s - lum * s
    p = 2 * lum - q

    def f(t):
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    return f(h + 1 / 3), f(h), f(h - 1 / 3)


def _palette_top(hex_color):
    """Guidens topp-färg: hslToRgb(h, s*.85, min(l+.25, .78))."""
    h, s, lum = _rgb_to_hsl(*_hex_to_rgb(hex_color))
    return _hsl_to_rgb(h, s * 0.85, min(lum + 0.25, 0.78))


def text_is_dark(hex_color):
    """True om sidans text ska vara mörk (ljus gradient-topp). Port av
    shouldTextBeDark: relativ luminans på härledda toppfärgen > 0.4."""
    try:
        rgb = _palette_top(hex_color)
    except (ValueError, IndexError):
        rgb = _palette_top(DEFAULT_PAGE_COLOR)

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b) > 0.4


def resolve_page_color(*candidates):
    """Första icke-tomma giltiga hexfärgen, annars default."""
    import re

    for candidate in candidates:
        if candidate and re.fullmatch(r"#[0-9a-fA-F]{6}", candidate.strip()):
            return candidate.strip().lower()
    return DEFAULT_PAGE_COLOR
