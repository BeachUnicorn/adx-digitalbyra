"""
Utvalda omdömen från Reco (Giovannis beslut 2026-10-04, reco.py).

FlamingoAccount.reco_reviews och reco_reviews_selected: omdömena från
kundens intygade profilsida på Reco och kundens val och ordning.
FlamingoSettings (en rad): byråns brytare som stänger av Utvalda för alla.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0014_reco_profilen"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_reviews",
            field=models.JSONField(blank=True, default=list, verbose_name="Omdömen från Reco"),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="reco_reviews_selected",
            field=models.JSONField(
                blank=True, default=list, verbose_name="Valda omdömen från Reco"
            ),
        ),
        migrations.CreateModel(
            name="FlamingoSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "reco_selected_enabled",
                    models.BooleanField(default=True, verbose_name="Utvalda omdömen från Reco"),
                ),
                (
                    "reco_selected_changed_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Utvalda omdömen från Reco ändrades"
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "reco_selected_changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Utvalda omdömen från Reco ändrades av",
                    ),
                ),
            ],
            options={
                "verbose_name": "Flamingos inställningar",
                "verbose_name_plural": "Flamingos inställningar",
            },
        ),
    ]
