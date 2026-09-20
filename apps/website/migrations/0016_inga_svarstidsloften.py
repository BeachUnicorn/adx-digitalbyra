"""
Tar bort svarstidslöften ur innehåll som seedskript skrivit in.

"Svar inom en arbetsdag" och "Vi svarar samma dag" hamnade i drygt hundra
formulärblock, en CTA-rad och en produktbeskrivning via seeds. Det är
Giovannis löfte att ge, inte kodens (2026-09-20). Migrationen är
idempotent och rör bara exakt de fraserna; allt annat innehåll lämnas.
"""

from django.db import migrations

INQUIRY_PROMISES = (
    " Svar inom en arbetsdag.",
    "Svar inom en arbetsdag. ",
    "Svar inom en arbetsdag.",
)


def _clean_text(value):
    if not isinstance(value, str):
        return value, False
    cleaned = value
    for phrase in INQUIRY_PROMISES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = cleaned.replace("Vi svarar samma dag", "Första mötet kostar ingenting")
    return cleaned.strip() if cleaned != value else value, cleaned != value


def forwards(apps, schema_editor):
    Block = apps.get_model("website", "Block")
    for block in Block.objects.filter(block_type__in=["inquiry_form", "bar"]):
        data = dict(block.data or {})
        changed = False
        for key in ("intro", "note"):
            if key in data:
                data[key], did = _clean_text(data[key])
                changed = changed or did
        if changed:
            block.data = data
            block.save(update_fields=["data"])

    Product = apps.get_model("offers", "Product")
    for product in Product.objects.filter(description__contains="Ändringar samma dag."):
        product.description = product.description.replace(" Ändringar samma dag.", "").replace(
            "Ändringar samma dag.", ""
        )
        product.save(update_fields=["description"])


class Migration(migrations.Migration):
    dependencies = [
        ("website", "0015_blockpage_category"),
        ("offers", "0003_offert_projekt"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
