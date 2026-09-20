"""
Andra svepet efter svarstidslöften (se 0016): kontaktformulärets "senast
nästa vardag", förvaltningssidans "inom två arbetsdagar" och dess FAQ-svar.
Exakta fraser, idempotent, allt annat innehåll orört.
"""

from django.db import migrations

REPLACEMENTS = (
    (
        "Fyll i formuläret så återkommer vi senast nästa vardag.",
        "Fyll i formuläret så återkommer vi.",
    ),
    (
        " Normala ändringar levereras inom två arbetsdagar, akuta fel tas direkt.",
        " Akuta fel går alltid först.",
    ),
)
FAQ_REPLACEMENT = (
    "Normala innehållsändringar görs inom två arbetsdagar. "
    "Akuta fel, som att sajten ligger nere, tar vi direkt.",
    "Innehållsändringar görs löpande. Akuta fel, som att sajten ligger nere, går alltid först.",
)


def _replace_in(value):
    if not isinstance(value, str):
        return value, False
    out = value
    for old, new in REPLACEMENTS:
        out = out.replace(old, new)
    return out, out != value


def forwards(apps, schema_editor):
    Block = apps.get_model("website", "Block")
    for block in Block.objects.filter(block_type__in=["inquiry_form", "prose"]):
        data = dict(block.data or {})
        changed = False
        for key in ("intro", "body"):
            if key in data:
                data[key], did = _replace_in(data[key])
                changed = changed or did
        if changed:
            block.data = data
            block.save(update_fields=["data"])

    FAQItem = apps.get_model("faq", "FAQItem")
    old, new = FAQ_REPLACEMENT
    for item in FAQItem.objects.filter(answer__contains="inom två arbetsdagar"):
        item.answer = item.answer.replace(old, new)
        item.save(update_fields=["answer"])


class Migration(migrations.Migration):
    dependencies = [
        ("website", "0016_inga_svarstidsloften"),
        ("faq", "0001_initial"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
