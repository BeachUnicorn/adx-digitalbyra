"""
Flerval i landningssidans formulär (Giovanni 2026-10-10, apps/flamingo/answers.py):

- Lead.choice_answers: svaren på flervalsfrågorna med alternativens
  nycklar och texterna som de stod, en post per besvarad fråga
  (db_default []). Svaret står också som text i Lead.answers, som förut.

Frågornas nya sorter (one, many) och fälten options och required bor i
blockens JSON och kräver ingen migrering.

B.0: den förra versionen skriver förfrågningar mellan migrate och reload,
och för gott efter en tillbakarullning, utan att känna till fältet. Därför
db_default. Inga främmande nycklar, så ingen raderingsregel i databasen.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0017_svar_pa_utskick"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="choice_answers",
            field=models.JSONField(
                blank=True,
                db_default=models.Value([], output_field=models.JSONField()),
                default=list,
                verbose_name="Flervalssvar",
            ),
        ),
    ]
