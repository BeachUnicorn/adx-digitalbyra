"""
Annonserna till sidans egen adress (README Steg 2, Giovanni 2026-10-28):
Campaign.ads_final_url, slutadressen annonserna publicerades med.

Tom för kampanjer som redan publicerats: deras annonser leder till
kampanjens adress /lp/<page_slug>/ och skrivs inte om. README för utskick
B.0: db_default "", så att den förra versionen kan skriva kampanjer mellan
migrate och reload.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0019_egna_adresser"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="ads_final_url",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=500,
                verbose_name="Annonsernas slutadress",
            ),
        ),
    ]
