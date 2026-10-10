"""
QR-koder som SVG och PNG (README I.7 Anmälan och I.11 Länkar, S4), med
segno (ren Python, ingen Pillow).

    FORMATS                         ("svg", "png")
    svg(data, *, scale=SCALE) -> bytes     med width, height och viewBox
    png(data, *, scale=SCALE) -> bytes
    response(data, fmt, *, filename, download=False) -> HttpResponse

Alltid en vanlig QR-kod (aldrig Micro QR, som många telefoner inte läser),
felkorrigering M (tål en fläck på en affisch) och en tyst zon på fyra
moduler, som standarden kräver. data är en absolut adress: den namngivna
länken (links.named_link_url) eller anmälningssidan; aldrig något
personligt. Används av app_views/links.py (link_qr och signup_qr).
"""

import io
import re

from django.http import Http404, HttpResponse

FORMATS = ("svg", "png")
#: Pixlar per modul. En kod med 29 moduler och tyst zon blir 370 px.
SCALE = 10
BORDER = 4
ERROR = "m"
CONTENT_TYPES = {"svg": "image/svg+xml", "png": "image/png"}
#: Filnamnet i Content-Disposition: bara säkra tecken.
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _code(data):
    import segno

    text = str(data or "").strip()
    if not text:
        raise ValueError("QR-koden behöver en adress")
    return segno.make_qr(text, error=ERROR, boost_error=False)


def svg(data, *, scale=SCALE):
    """QR-koden som SVG: svart på vitt, med width och height (så att filen
    öppnas i rätt storlek) och viewBox (så att den skalar i en sida)."""
    code = _code(data)
    out = io.BytesIO()
    code.save(
        out,
        kind="svg",
        scale=scale,
        border=BORDER,
        dark="#000",
        light="#fff",
        xmldecl=False,
        svgns=True,
        svgclass=None,
        lineclass=None,
    )
    width, height = code.symbol_size(scale=scale, border=BORDER)
    text = out.getvalue().decode("ascii")
    text = text.replace("<svg ", f'<svg viewBox="0 0 {width} {height}" ', 1)
    return text.encode("ascii")


def png(data, *, scale=SCALE):
    """QR-koden som PNG, svart på vitt."""
    out = io.BytesIO()
    _code(data).save(out, kind="png", scale=scale, border=BORDER, dark="#000", light="#fff")
    return out.getvalue()


def response(data, fmt, *, filename, download=False):
    """HttpResponse med QR-koden i formatet fmt ("svg" eller "png"; annat
    ger 404). filename utan ändelse; download=True ger en nedladdning."""
    if fmt not in FORMATS:
        raise Http404
    body = svg(data) if fmt == "svg" else png(data)
    response = HttpResponse(body, content_type=CONTENT_TYPES[fmt])
    name = _FILENAME_RE.sub("-", str(filename or "qr")).strip("-.") or "qr"
    disposition = "attachment" if download else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{name}.{fmt}"'
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response
