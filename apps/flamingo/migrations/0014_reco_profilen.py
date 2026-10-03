"""
Kundens profil på Reco (Giovannis beslut 2026-10-04, reco.py).

FlamingoAccount.reco_*: Recos id för företaget, länken till profilen, namnet,
betyget och antalet (bara för verktyget), när profilen hämtades, och om den
liknar företaget (reco_unverified) eller är intygad (reco_confirmed_at och
_by). Blocket "Omdömen från Reco" visar Recos egen ruta, byggd bara av id:t.
Ett id hör till en kund (flamingo_reco_id_unique, demot undantaget). Inga
omdömestexter sparas.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0013_sidans_ursprung_och_profilen"),
        ("projects", "0011_bort_med_kolumnerna"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_confirmed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Reco-profilen intygad"),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Reco-profilen intygad av",
            ),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_fetched_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Reco-profilen hämtad"),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_name",
            field=models.CharField(blank=True, max_length=200, verbose_name="Namnet på Reco"),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_rating",
            field=models.DecimalField(
                blank=True, decimal_places=1, max_digits=2, null=True, verbose_name="Betyg på Reco"
            ),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_review_count",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="Antal omdömen på Reco"
            ),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_unverified",
            field=models.BooleanField(
                default=False,
                help_text="Inget från Reco syns på sidorna förrän någon intygat att profilen är kundens.",
                verbose_name="Reco-profilen liknar inte företaget",
            ),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_url",
            field=models.URLField(blank=True, max_length=300, verbose_name="Profilen på Reco"),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_venue_id",
            field=models.CharField(
                blank=True, max_length=12, verbose_name="Recos id för företaget"
            ),
        ),
        migrations.AddConstraint(
            model_name="flamingoaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    models.Q(("reco_venue_id", ""), _negated=True), ("is_demo", False)
                ),
                fields=("reco_venue_id",),
                name="flamingo_reco_id_unique",
            ),
        ),
    ]
