"""
Flamingos steg 2 (apps/flamingo/README.md, Steg 2, Giovanni 2026-10-28):
länkar direkt till en landningssida och den namngivna länkens sort.

- TrackedLink.landing_page (null): en länk av slaget lp till sidans egen
  adress /lp/<konto>/<sida>/, utan kampanj.
- TrackedLink.medium (db_default ""): var en namngiven länk används, QR-kod,
  Instagram eller Annat (tomt räknas som Annat).

B.0: den förra versionen skriver länkar utan fälten, därför null och
db_default. Den främmande nyckeln får sin SET NULL också i databasen
(dbfk.py), så att den förra versionen kan ta bort en sida.
"""

import django.db.models.deletion
from django.db import migrations, models

from apps.utskick import dbfk

#: Varje främmande nyckel som migreringen skapar.
FOREIGN_KEYS = [
    ("utskick", "trackedlink", "landing_page"),
]


def on_delete_in_database(apps, schema_editor):
    dbfk.apply(apps, schema_editor, FOREIGN_KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0019_egna_adresser"),
        ("utskick", "0005_gamla_adresser"),
    ]

    operations = [
        migrations.AddField(
            model_name="trackedlink",
            name="landing_page",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="utskick_links",
                to="flamingo.landingpage",
            ),
        ),
        migrations.AddField(
            model_name="trackedlink",
            name="medium",
            field=models.CharField(
                blank=True,
                choices=[("qr", "QR-kod"), ("instagram", "Instagram"), ("other", "Annat")],
                db_default="",
                default="",
                max_length=10,
            ),
        ),
        migrations.RunPython(on_delete_in_database, migrations.RunPython.noop),
    ]
