"""
Tar bort sms-tjänstens namn ur integrationsraden på /flamingo/.

seed_flamingo skrev "Sms via 46elks" i fl_logos-blocket (Giovanni
2026-10-10: vilka leverantörer ADX använder är privat). Seedfilen är
rättad, men raden ligger kvar i databasen, och seed_flamingo kan inte köras
om: den raderar sidornas block och skriver över allt som ändrats sedan.

Migrationen är idempotent och rör bara exakt den posten i listan; allt
annat innehåll, också en post som ändrats för hand, lämnas.
"""

from django.db import migrations

OLD = "Sms via 46elks"
NEW = "Sms"


def forwards(apps, schema_editor):
    Block = apps.get_model("website", "Block")
    for block in Block.objects.filter(block_type="fl_logos"):
        data = dict(block.data or {})
        items = data.get("items")
        if not isinstance(items, list) or OLD not in items:
            continue
        data["items"] = [NEW if item == OLD else item for item in items]
        block.data = data
        block.save(update_fields=["data"])


class Migration(migrations.Migration):
    dependencies = [("website", "0019_flamingoblock")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
