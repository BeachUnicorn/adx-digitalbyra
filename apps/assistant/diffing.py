"""
Ordnivå-diff för historik- och utkastvyerna.

Standardbibliotekets difflib räcker: vi diffar per fält, på ord, och
renderar <ins>/<del>. Rich-fält (HTML) diffas på sin textrepresentation för
läsbarhet - HTML-diffar är oläsliga för människor.
"""

import difflib
import re

from django.utils.html import escape, strip_tags
from django.utils.safestring import mark_safe

_TOKEN = re.compile(r"\s+|[^\s]+")

#: Fält som aldrig är intressanta i en diff.
SKIP_FIELDS = {"id", "created_at", "updated_at", "lft", "rght", "tree_id", "level"}

#: Fält vars värde är HTML - diffas som text.
RICH_FIELDS = {"body", "answer", "description"}


def _tokens(text):
    return _TOKEN.findall(text or "")


def diff_html(old, new):
    """Ordnivå-diff av två strängar -> säker HTML med <ins>/<del>."""
    matcher = difflib.SequenceMatcher(a=_tokens(str(old or "")), b=_tokens(str(new or "")))
    parts = []
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        old_chunk = escape("".join(matcher.a[a1:a2]))
        new_chunk = escape("".join(matcher.b[b1:b2]))
        if op == "equal":
            parts.append(new_chunk)
        elif op == "delete":
            parts.append(f"<del>{old_chunk}</del>")
        elif op == "insert":
            parts.append(f"<ins>{new_chunk}</ins>")
        else:  # replace
            parts.append(f"<del>{old_chunk}</del><ins>{new_chunk}</ins>")
    return mark_safe(parts and "".join(parts) or "")


def _display(field, value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Ja" if value else "Nej"
    text = str(value)
    if field in RICH_FIELDS:
        text = strip_tags(text)
    return text


def field_diffs(before, after, labels=None):
    """
    Två fält-dictar -> [{field, label, old, new, diff}] för ändrade fält.

    `before` kan vara None (nytt objekt) - då blir allt insättning.
    """
    labels = labels or {}
    before = before or {}
    rows = []
    for field in after:
        if field in SKIP_FIELDS:
            continue
        old = _display(field, before.get(field))
        new = _display(field, after.get(field))
        if old == new:
            continue
        rows.append(
            {
                "field": field,
                "label": labels.get(field, field.replace("_", " ").capitalize()),
                "old": old,
                "new": new,
                "diff": diff_html(old, new),
            }
        )
    return rows
