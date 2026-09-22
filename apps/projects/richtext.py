"""
Ärendebeskrivningen är HTML (skriven i Tiptap, data-tiptap="issue").

Allt som skrivs till Issue.description går genom sanitize_issue_html, vad
det än kommer ifrån: portalens formulär, tavlans panel, MCP. Tillåtet är
exakt vad verktygsraden kan göra: stycken, fet, genomstruken, markering,
länk, listor, tabell och textjustering. Justeringen är det enda
style-attributet, och bara värdena text-align: left/center/right.

Textversionen (html_to_text) används där HTML inte passar: mejl,
kortets utdrag, sökning och det AI:n läser.
"""

import html as html_module
import re

import nh3

from apps.common.security import normalize_typography

MAX_LENGTH = 40000

_TAGS = {
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "s",
    "del",
    "mark",
    "a",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}
_ATTRIBUTES = {
    "a": {"href", "title", "target"},
    "p": {"style"},
    "th": {"colspan", "rowspan", "style"},
    "td": {"colspan", "rowspan", "style"},
}
_URL_SCHEMES = {"http", "https", "mailto", "tel"}
_ALIGN_RE = re.compile(r"^\s*text-align\s*:\s*(left|center|right)\s*;?\s*$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"^[1-9]\d?$")


def _attribute_filter(tag, attribute, value):
    if attribute == "style":
        match = _ALIGN_RE.match(value)
        return f"text-align: {match.group(1).lower()}" if match else None
    if attribute in ("colspan", "rowspan"):
        return value if _NUMBER_RE.match(value) else None
    return value


def sanitize_issue_html(raw):
    if not raw:
        return ""
    text = str(raw)
    # Ren text (från MCP eller äldre poster) blir stycken i stället för att
    # radbrytningarna försvinner.
    if "<" not in text:
        text = text_to_html(text)
    cleaned = nh3.clean(
        text,
        tags=_TAGS,
        attributes=_ATTRIBUTES,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
        attribute_filter=_attribute_filter,
    )
    cleaned = normalize_typography(cleaned).strip()
    if not html_to_text(cleaned).strip() and "<table" not in cleaned:
        return ""
    return cleaned[:MAX_LENGTH]


def text_to_html(text):
    """Ren text -> stycken (tom rad) och radbrytningar (<br>)."""
    text = str(text or "").replace("\r", "").strip()
    if not text:
        return ""
    paragraphs = re.split(r"\n{2,}", text)
    return "".join("<p>" + html_module.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)


def html_to_text(html):
    """HTML -> läsbar text: stycken blir tomrader, listpunkter får streck, celler tabb."""
    if not html:
        return ""
    text = str(html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</(li|tr|ul|ol|table|thead|tbody)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
