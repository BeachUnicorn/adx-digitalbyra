"""
Brevs textversion (README F.4): byggd ur samma block och samma underlag som
HTML-versionen (render.block_views), aldrig ur HTML:en.

    render_text(utskick, ctx) -> str
        rubriker på egen rad, stycken med en tom rad emellan, knappar och
        länkar som "Boka service: https://klick.adx.se/m/..." (spårade på
        samma sätt som i HTML-versionen), listor med "- ", sidfotens namn,
        adress, skäl och länkar utskrivna. Utskick.text_override vinner när
        kunden skrivit en egen textversion: den sammanfogas, och "Visa i
        webbläsaren" och sidfoten läggs ändå på.
    default_text(utskick, ctx) -> str
        textversionen ur blocken, utan text_override (redigerarens
        "Återställ textversionen")

Värden är råa (inte HTML-escapade) i texten (F.3). Raderna slutar med \\n;
mime.build kodar dem som quoted-printable.
"""

from . import render

#: Avdelaren och sidfotens linje i texten.
RULE = "-" * 24


def _join(*parts):
    return "\n".join(p for p in parts if p)


def _link(label, href):
    if not href:
        return label or ""
    return f"{label}: {href}" if label else href


def _block_text(kind, v):
    """Ett block som text (vyn ur render._prepare)."""
    if kind == "hero":
        button = _link(v.get("button_text"), v.get("button_href")) if v.get("button_href") else ""
        return _join(
            (v.get("kicker") or "").upper(),
            v.get("title"),
            "\n" + v["lead"] if v.get("lead") else "",
            "\n" + button if button else "",
        )
    if kind == "heading":
        return v.get("text") or ""
    if kind in ("text", "callout"):
        return render.rich_text(v.get("rich") or [])
    if kind == "button":
        lines = []
        for key in ("primary", "secondary"):
            button = v.get(key)
            if button:
                lines.append(_link(button["text"], button["href"]))
        return _join(*lines)
    if kind == "image":
        image = v.get("image") or {}
        caption = v.get("caption") or image.get("alt") or ""
        if v.get("href"):
            return _link(caption or "Bild", v["href"])
        return caption
    if kind == "image_text":
        link = _link(v.get("link_text"), v.get("link_href")) if v.get("link_href") else ""
        return _join(v.get("title"), v.get("body"), link)
    if kind == "columns":
        return "\n\n".join(
            _join(" ".join(p for p in (c.get("number"), c.get("title")) if p), c.get("text"))
            for c in v.get("columns") or []
        )
    if kind == "divider":
        return RULE
    if kind == "offer":
        return _join(
            v.get("valid"),
            v.get("title"),
            v.get("text"),
            f"Kod: {v['code']}" if v.get("code") else "",
        )
    if kind == "prices":
        rows = [
            f"{r['name']}: {r['price']}" if r.get("price") else r["name"]
            for r in v.get("rows") or []
        ]
        return _join(v.get("title"), *rows, v.get("note"))
    if kind == "reviews":
        lines = []
        if v.get("summary"):
            lines.append(f"{v.get('rating')} av 5 · {v.get('count_text')}")
        for quote in v.get("quotes") or []:
            author = f" ({quote['author']})" if quote.get("author") else ""
            lines.append(f"{quote['text']}{author}")
        return "\n\n".join(lines)
    if kind == "steps":
        steps = [f"{s['n']}. {render.rich_text(s['rich'])}" for s in v.get("steps") or []]
        return _join(v.get("title"), *steps)
    if kind == "event":
        calendar = _link("Lägg till i kalendern", v.get("calendar_href"))
        return _join(
            v.get("title"),
            v.get("line"),
            v.get("place"),
            calendar if v.get("calendar_href") else "",
        )
    if kind == "person":
        contact = " · ".join(p for p in (v.get("phone"), v.get("email")) if p)
        return _join(v.get("name"), v.get("role"), contact)
    if kind == "video":
        return _link(v.get("title") or "Se videon", v.get("href"))
    if kind == "gallery":
        return ""
    if kind == "faq":
        items = [_join(i.get("q"), i.get("a")) for i in v.get("items") or []]
        return "\n\n".join([p for p in [v.get("title")] if p] + items)
    if kind == "hours":
        rows = [f"{r['label']} {r['value']}".strip() for r in v.get("rows") or []]
        address = v.get("address") or ""
        maps = _link(v.get("map_text"), v.get("map_href")) if v.get("map_href") else ""
        return _join("Öppettider" if rows else "", *rows, address, maps)
    if kind == "signature":
        line = " · ".join(p for p in (v.get("line"), v.get("phone")) if p)
        return _join(v.get("greeting"), v.get("name"), line)
    if kind == "spacer":
        return ""
    if kind == "social":
        return _join(*(_link(link["label"], link["href"]) for link in v.get("links") or []))
    return ""


def _footer(utskick, ctx):
    name, lines = render.company_lines(ctx)
    links = [
        _link(text, url)
        for text, url in render.footer_links(utskick, ctx)
        if text != render.WEB_VIEW_TEXT
    ]
    # En uppgift per rad, och en tom rad före länkarna (Giovanni 2026-10-10).
    return "\n\n".join(p for p in (_join(RULE, name, *lines), _join(*links)) if p)


def _web_line(utskick, ctx):
    if ctx.web:
        return ""
    return _link(render.WEB_VIEW_TEXT, render.web_url(utskick, ctx))


def _tidy(text):
    """Högst en tom rad i följd, inga blanksteg sist på raderna."""
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    out = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


def default_text(utskick, ctx):
    """Textversionen ur blocken, utan kundens egen text."""
    parts = []
    for entry, block_type, view in render.block_views(utskick, ctx):
        text = _block_text(block_type.key, view)
        if text.strip():
            parts.append(text)
    body = "\n\n".join(parts)
    return _tidy(
        "\n\n".join(p for p in (_web_line(utskick, ctx), body, _footer(utskick, ctx)) if p)
    )


def render_text(utskick, ctx):
    """Textversionen som skickas: kundens egen (sammanfogad) när den finns,
    annars den ur blocken. Länken till webbversionen och sidfoten finns
    alltid med."""
    override = str(getattr(utskick, "text_override", "") or "").strip()
    if not override:
        return default_text(utskick, ctx)
    body = ctx.m(override)
    return _tidy(
        "\n\n".join(p for p in (_web_line(utskick, ctx), body, _footer(utskick, ctx)) if p)
    )
