"""
Rendering av chattmeddelanden.

Modellen svarar i markdown - listor, fetstil, rubriker - och utan rendering
läste kunden råa asterisker. Här görs markdown till HTML, saneras och får
@-tokens utbytta mot chips.

Ordningen är säkerhetskritisk:

1. markdown -> HTML (modellens text är INTE betrodd; den kan innehålla
   text som en webbsida matat in via ett läsverktyg)
2. nh3 saneras mot en snäv taggista - samma boundary som resten av sajten
3. chips ersätts sist, med format_html på redan sanerad HTML

Taggistan är medvetet snäv. Assistenten ska svara, inte formge: inga
bilder, ingen inbäddning, inga tabeller. Rubriker mappas ned till <strong>
eftersom h1-h3 i en chattbubbla konkurrerar med sidans egen rubriknivå.
"""

import re

import markdown as md
import nh3
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .mentions import TOKEN, TYPE_LABELS

#: Vad assistentens svar får innehålla efter sanering.
ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "a",
    "blockquote",
    "hr",
}
#: "rel" utelämnas med flit: nh3 sätter den själv via link_rel, och
#: att lista den där är ett fel.
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}

#: Rubriker i en chattbubbla krockar med sidans rubriknivåer - platta ut
#: dem till fet text i stället för att tillåta h1-h6.
_HEADING = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.DOTALL)


def _chip(match):
    typ, ref = match.group(1), match.group(2)
    return format_html(
        '<span class="m-chip" data-typ="{}">{}<b>{}</b></span>',
        typ,
        TYPE_LABELS.get(typ, typ) + " ",
        ref.replace("-", " "),
    )


#: Granskningslänken klistrades tidigare in som ren text i svaret. Den
#: raden ersätts nu av en knapp - städa bort den ur redan sparade
#: meddelanden så gamla samtal inte visar en oklickbar URL.
_LEGACY_REVIEW_LINE = re.compile(r"\n*\d+\s+utkast väntar på ditt godkännande:\s*\S+\s*$")


def message_html(text):
    """Assistentens svar som säker HTML."""
    if not text:
        return ""
    text = _LEGACY_REVIEW_LINE.sub("", text)
    html = md.markdown(
        text,
        extensions=["fenced_code", "nl2br", "sane_lists"],
        output_format="html",
    )
    html = _HEADING.sub(r"<p><strong>\1</strong></p>", html)
    html = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer",
    )
    return mark_safe(TOKEN.sub(_chip, html))


#: Markdown-markörer som ska bort ur berättarstegen. De visas i litet,
#: kursivt format där rendering vore överdrivet - men råa asterisker ser
#: ut som ett fel.
_INLINE_MD = re.compile(r"(\*\*|__|\*|_|`|^#{1,6}\s*)", re.MULTILINE)


def inline_text(text):
    """Berättarsteg som ren text: markdown-markörerna bort, inget annat."""
    if not text:
        return ""
    return _INLINE_MD.sub("", text).strip()
