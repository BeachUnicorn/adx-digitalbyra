"""
Mallarnas hjälpare för sidbyggarens block (templates/flamingo/lp/ren/).

    {% pb "title" %}               data-pb-field="title" i redigeringsläget
    {% pb "steps" i "title" %}     data-pb-field="steps.0.title"
    {% pb_media "image" %}         data-pb-field="image" data-pb-media
    {% pb_block %}                 blockets data-pb-block, -type, -variant, -version
    {% pb_empty "lead" "p" "rn-lead" %}
                                   ett tomt element för ett tomt fält (bara
                                   i redigeringsläget, dolt med CSS)
    {% pb_empty_path "steps" i "text" "p" "rn-step__text" %}
                                   samma för ett underfält i en lista
                                   (data-pb-field="steps.0.text"); de tre
                                   första är sökvägen, sedan tagg och klass
    {% rn_img asset sizes="..." eager=False cls="" %}
                                   en bild ur mediaarkivet med alt, bredd,
                                   höjd, srcset och loading=lazy
    {{ value|rn_sv_decimal }}      4.8 som "4,8"

Utan editing i kontexten ger pb-taggarna ingenting: den publika sidan har
inga redigeringsattribut. Allt escapas med format_html.
"""

from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import format_html, format_html_join

from ..models import MEDIA_THUMB_SIDE

register = template.Library()


def _editing(context):
    return bool(context.get("editing"))


@register.simple_tag(takes_context=True)
def pb(context, *path):
    if not _editing(context):
        return ""
    return format_html(' data-pb-field="{}"', ".".join(str(p) for p in path))


@register.simple_tag(takes_context=True)
def pb_media(context, key):
    if not _editing(context):
        return ""
    return format_html(' data-pb-field="{}" data-pb-media', key)


@register.simple_tag(takes_context=True)
def pb_block(context):
    if not _editing(context):
        return ""
    b = context.get("b") or {}
    return format_html(
        ' data-pb-block="{}" data-pb-type="{}" data-pb-variant="{}" data-pb-version="{}"',
        b.get("id", ""),
        b.get("type", ""),
        b.get("variant", ""),
        b.get("version", ""),
    )


@register.simple_tag(takes_context=True)
def pb_empty(context, key, tag="p", cls=""):
    """Ett tomt fält i redigeringsläget: ett tomt element med fältets
    etikett som platshållare (data-pb-placeholder). Dolt med CSS tills
    redigeraren visar det, så layouten är densamma."""
    if not _editing(context):
        return ""
    b = context.get("b") or {}
    spec = (b.get("fields") or {}).get(key.split(".")[0])
    label = spec.label if spec is not None else key
    tag = tag if tag in ("p", "h1", "h2", "h3", "span", "div", "li") else "p"
    return format_html(
        '<{tag} class="{cls}" data-pb-field="{key}" data-pb-empty data-pb-placeholder="{label}">'
        "</{tag}>",
        tag=tag,
        cls=cls,
        key=key,
        label=label,
    )


@register.simple_tag(takes_context=True)
def pb_empty_path(context, key, index, sub, tag="p", cls=""):
    """pb_empty för ett underfält i en lista ("steps", 0, "text"): ett tomt
    element med underfältets etikett, så att det går att fylla i på plats."""
    if not _editing(context):
        return ""
    b = context.get("b") or {}
    spec = (b.get("fields") or {}).get(key)
    sub_spec = spec.sub(sub) if spec is not None and hasattr(spec, "sub") else None
    label = sub_spec.label if sub_spec is not None else sub
    tag = tag if tag in ("p", "h3", "h4", "span", "div") else "p"
    return format_html(
        '<{tag} class="{cls}" data-pb-field="{path}" data-pb-empty data-pb-placeholder="{label}">'
        "</{tag}>",
        tag=tag,
        cls=cls,
        path=f"{key}.{index}.{sub}",
        label=label,
    )


#: Miniatyrens längsta sida (media.py).
THUMB_SIDE = MEDIA_THUMB_SIDE


def _thumb_width(width, height):
    """Miniatyrens bredd: miniatyren ryms i THUMB_SIDE på den längsta sidan
    (Pillow thumbnail), så en stående bild är smalare än THUMB_SIDE."""
    if not width:
        return THUMB_SIDE
    longest = max(width, height or 0)
    return round(width * min(1, THUMB_SIDE / longest))


@register.simple_tag
def rn_img(asset, sizes="100vw", eager=False, cls="", alt=None):
    """<img> för ett MediaAsset: alt, width, height, srcset med miniatyren
    och loading=lazy (eager för Toppen: fetchpriority=high i stället)."""
    if asset is None or not asset.file:
        return ""
    srcset = []
    if asset.thumb:
        thumb_width = _thumb_width(asset.width, asset.height)
        if not asset.width or thumb_width < asset.width:
            srcset.append((asset.thumb.url, thumb_width))
    if asset.width:
        srcset.append((asset.file.url, asset.width))
    attrs = {
        "src": asset.file.url,
        "alt": asset.alt if alt is None else alt,
        "width": asset.width or None,
        "height": asset.height or None,
        "class": cls or None,
        "decoding": "async",
    }
    if eager:
        attrs["fetchpriority"] = "high"
    else:
        attrs["loading"] = "lazy"
    if len(srcset) > 1:
        attrs["srcset"] = ", ".join(f"{url} {w}w" for url, w in srcset)
        attrs["sizes"] = sizes
    parts = format_html_join(
        "", ' {}="{}"', ((k, v) for k, v in attrs.items() if v is not None and v != "")
    )
    alt_missing = ' alt=""' if not attrs["alt"] else ""
    return format_html("<img{}{}>", parts, alt_missing)


@register.filter
def rn_sv_decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value
    return f"{number:.1f}".replace(".", ",")
