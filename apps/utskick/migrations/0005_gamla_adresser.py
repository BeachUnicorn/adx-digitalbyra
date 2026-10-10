"""
Flamingo 2.0, steg S4 (säkerhetsgranskningen): de tidigare adresserna för
anmälan (OldPublicSlug).

En public_slug som byts kan inte längre gå till en annan kund: de
namngivna länkarna klick.adx.se/<adress>/<slug> och QR-koden till
anmälningssidan står kvar på tryckta affischer. Den gamla adressen sparas
här och följer kontot.

B.0: en ny tabell, inget fält på en äldre tabell. Den främmande nyckeln får
sin SET NULL också i databasen (dbfk.py), så att en äldre version kan ta
bort ett konto.
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

from apps.utskick import dbfk

#: Varje främmande nyckel som migreringen skapar.
S4_FOREIGN_KEYS = [
    ("utskick", "oldpublicslug", "account"),
]


def on_delete_in_database(apps, schema_editor):
    dbfk.apply(apps, schema_editor, S4_FOREIGN_KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0017_svar_pa_utskick"),
        ("utskick", "0004_segment_och_skript"),
    ]

    operations = [
        migrations.CreateModel(
            name="OldPublicSlug",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "slug",
                    models.SlugField(max_length=40, unique=True, verbose_name="Tidigare adress"),
                ),
                ("retired_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="utskick_old_slugs",
                        to="flamingo.flamingoaccount",
                    ),
                ),
            ],
            options={
                "verbose_name": "Tidigare adress för anmälan",
                "verbose_name_plural": "Tidigare adresser för anmälan",
                "ordering": ["-retired_at", "pk"],
            },
        ),
        migrations.RunPython(on_delete_in_database, migrations.RunPython.noop),
    ]
