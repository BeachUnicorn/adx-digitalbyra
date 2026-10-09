"""
Lead.contact: förfrågans kontakt i kundens register (apps/utskick, B.7).

Nullbar (B.0): den förra versionen skriver förfrågningar mellan migrate och
reload, och efter en återställning för gott, utan att känna till fältet.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0015_utvalda_omdomen_fran_reco"),
        ("utskick", "0001_kontakter"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="contact",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="leads",
                to="utskick.contact",
                verbose_name="Kontakt",
            ),
        ),
    ]
