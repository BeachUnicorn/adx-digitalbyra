from django import template

from apps.projects.board import fmt_seconds

register = template.Library()


@register.filter
def hms(seconds):
    return fmt_seconds(seconds)


@register.simple_tag
def field_row(field, full=False):
    """En formulärrad i sajtens .adx-form-stil."""
    from django.utils.html import format_html

    css = "field full" if full or field.field.widget.input_type in ("textarea", "file") else "field"
    if getattr(field.field.widget, "input_type", "") == "checkbox":
        return format_html(
            '<div class="{}"><label class="check">{} <span>{}</span></label>{}</div>',
            css,
            field,
            field.label,
            field.errors,
        )
    return format_html(
        '<div class="{}"><label for="{}">{}</label>{}{}{}</div>',
        css,
        field.id_for_label,
        field.label,
        field,
        format_html('<small class="form-note">{}</small>', field.help_text)
        if field.help_text
        else "",
        field.errors,
    )
