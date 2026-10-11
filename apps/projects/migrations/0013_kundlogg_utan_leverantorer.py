"""
Två rader i kundloggen (som kunden ser i portalen) namngav ADX:s
leverantörer: DNS-tjänsten och felrapporteringen. Giovanni 2026-10-11: ADX
röjer inte sina leverantörer när det inte är nödvändigt.

Byter bara exakt de fraserna och är idempotent. "Claude Desktop" i en annan
rad lämnas: det är kundens egen app, som kunden redigerar sajten från.
"""

from django.db import migrations

REPLACEMENTS = (
    (
        "Route 53: SPF, DKIM och DMARC satt till p=reject.",
        "DNS: SPF, DKIM och DMARC satt till p=reject.",
    ),
    ("Sentry-felrapportering", "felrapportering"),
)


def forwards(apps, schema_editor):
    CustomerLogEntry = apps.get_model("projects", "CustomerLogEntry")
    for old, new in REPLACEMENTS:
        for entry in CustomerLogEntry.objects.filter(text__contains=old):
            entry.text = entry.text.replace(old, new)
            entry.save(update_fields=["text"])


class Migration(migrations.Migration):
    dependencies = [("projects", "0012_kontaktaktivitet")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
