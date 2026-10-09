"""
Förfrågan och utskicken (apps/utskick/README.md, B.7 och J S2):

- Lead.utskick och Lead.utskick_recipient: utskicket och mottagaren en
  förfrågan kom via (nullbara);
- Lead.attribution: ögonblicksbilden av spåret (db_default {});
- Lead.activity_at: senaste händelsen, som Inkorgen sorteras på
  (db_default now()), satt till created_at för alla befintliga förfrågningar
  i samma migrering, och ett index (konto, -activity_at, -id);
- källan "reply" (Svar på utskick): bara ett val, ingen kolumnändring.

B.0: den förra versionen skriver förfrågningar mellan migrate och reload,
och för gott efter en tillbakarullning, utan att känna till fälten. Därför
nullbart eller db_default, och raderingsregeln för de två främmande
nycklarna ligger också i databasen (apps/utskick/dbfk.py).
"""

import django.db.models.deletion
import django.db.models.functions.datetime
import django.utils.timezone
from django.db import migrations, models

from apps.utskick import dbfk

FOREIGN_KEYS = [
    ("flamingo", "lead", "utskick"),
    ("flamingo", "lead", "utskick_recipient"),
]


def on_delete_in_database(apps, schema_editor):
    dbfk.apply(apps, schema_editor, FOREIGN_KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0016_forfragans_kontakt"),
        ("utskick", "0002_utskick"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="activity_at",
            field=models.DateTimeField(
                db_default=django.db.models.functions.datetime.Now(),
                default=django.utils.timezone.now,
                verbose_name="Senast",
            ),
        ),
        # Befintliga förfrågningar: senast aktiv när de kom in. Nya rader
        # från den förra versionen får now() av databasen.
        migrations.RunSQL(
            "UPDATE flamingo_lead SET activity_at = created_at",
            migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name="lead",
            name="attribution",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, output_field=models.JSONField()),
                default=dict,
                verbose_name="Spår från utskick",
            ),
        ),
        migrations.AddField(
            model_name="lead",
            name="utskick",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="leads",
                to="utskick.utskick",
                verbose_name="Utskick",
            ),
        ),
        migrations.AddField(
            model_name="lead",
            name="utskick_recipient",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="leads",
                to="utskick.recipient",
            ),
        ),
        migrations.AlterField(
            model_name="lead",
            name="source",
            field=models.CharField(
                choices=[
                    ("form", "Formulär"),
                    ("call", "Samtal"),
                    ("manual", "Manuell"),
                    ("call_click", "Klick på telefonnumret"),
                    ("reply", "Svar på utskick"),
                ],
                default="form",
                max_length=10,
                verbose_name="Källa",
            ),
        ),
        migrations.AddIndex(
            model_name="lead",
            index=models.Index(
                fields=["account", "-activity_at", "-id"], name="flamingo_lead_activity"
            ),
        ),
        migrations.RunPython(on_delete_in_database, migrations.RunPython.noop),
    ]
