"""Template tags for FAQ schema.org structured data."""

import json

from django import template
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def faq_schema_json(faq_items, section_title=None):
    """
    Output a <script type="application/ld+json"> block with FAQPage schema.

    Usage:
        {% load faq_tags %}
        {% faq_schema_json faq_items "Vattenburna system" %}
    """
    if not faq_items:
        return ""

    entities = []
    for item in faq_items:
        if not getattr(item, "is_active", True):
            continue
        q = strip_tags(getattr(item, "question", str(item))).strip()
        a = strip_tags(getattr(item, "answer", "")).strip()
        if q and a:
            entities.append(
                {
                    "@type": "Question",
                    "name": q,
                    "acceptedAnswer": {"@type": "Answer", "text": a},
                }
            )

    if not entities:
        return ""

    data = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": entities,
    }
    if section_title:
        data["name"] = str(section_title)

    return mark_safe(
        '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>"
    )


@register.simple_tag
def load_faq_items(section_id):
    """
    Load FAQ items for a section ID. Returns a queryset or empty list.

    Usage:
        {% load faq_tags %}
        {% load_faq_items faq_section_id as faq_items %}
    """
    if not section_id:
        return []
    from apps.faq.models import FAQItem

    try:
        return list(
            FAQItem.objects.filter(section_id=int(section_id), is_active=True).order_by(
                "order", "id"
            )
        )
    except (ValueError, TypeError):
        return []


@register.simple_tag
def load_faq_section(section_id):
    """
    Load a FAQSection by ID. Returns the object or None.

    Usage:
        {% load faq_tags %}
        {% load_faq_section faq_section_id as faq_section %}
    """
    if not section_id:
        return None
    from apps.faq.models import FAQSection

    try:
        return FAQSection.objects.get(pk=int(section_id), is_active=True)
    except (FAQSection.DoesNotExist, ValueError, TypeError):
        return None
