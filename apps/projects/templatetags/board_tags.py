from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.projects.board import fmt_hours, fmt_seconds

register = template.Library()


@register.filter
def hms(seconds):
    return fmt_seconds(seconds)


@register.filter
def hours(seconds):
    """Timmar:minuter, för summor."""
    return fmt_hours(seconds)


@register.filter
def as_minutes(seconds):
    return int(round((seconds or 0) / 60))


@register.filter
def minutes_as_hours(minutes):
    return fmt_hours((minutes or 0) * 60)


@register.simple_tag
def m_field(field, wide=False):
    """En formulärrad i panelens .m-form-stil (label över fält, hjälptext, fel)."""
    widget = field.field.widget
    if getattr(widget, "input_type", "") == "checkbox":
        return format_html(
            '<div class="m-field m-field--check{}"><label>{} <span>{}</span></label>{}</div>',
            " m-field--wide" if wide else "",
            field,
            field.label,
            field.errors,
        )
    help_text = (
        format_html('<p class="m-field-help">{}</p>', field.help_text) if field.help_text else ""
    )
    return format_html(
        '<div class="m-field{}"><label for="{}">{}</label>{}{}{}</div>',
        " m-field--wide" if wide else "",
        field.id_for_label,
        field.label,
        field,
        help_text,
        field.errors,
    )


@register.filter
def issue_html(value):
    """Ärendebeskrivningen som HTML - saneras en gång till vid visning, för säkerhets skull."""
    from apps.projects.richtext import sanitize_issue_html

    return mark_safe(sanitize_issue_html(value))  # noqa: S308 - nyss sanerat
