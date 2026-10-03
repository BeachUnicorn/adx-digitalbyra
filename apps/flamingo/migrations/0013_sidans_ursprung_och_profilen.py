"""
Sidans ursprung och Google-profilens ägare.

LandingPage.built_for och built_rev: kampanjen vars förslag byggde sidan,
och utkastets rev då. Ett nytt förslag bygger om sidan bara för den
kampanjen och bara så länge ingen sparat något på sidan sedan dess
(pagebuilder.refresh_from_proposal). Sidor som redan finns får inget
ursprung, så de byggs aldrig om av ett nytt förslag.

FlamingoAccount.google_place_unverified, google_place_confirmed_at och _by:
en Google-profil som inte liknar företaget visar inga omdömen och inget
betyg förrän kunden eller byrån intygat att den är kundens (reviews.py).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0012_mediaarkivet_och_omdomen"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="landingpage",
            name="built_for",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="flamingo.campaign",
                verbose_name="Byggd av förslaget för",
            ),
        ),
        migrations.AddField(
            model_name="landingpage",
            name="built_rev",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="Utkastets rev när förslaget byggde det"
            ),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="google_place_unverified",
            field=models.BooleanField(
                default=False,
                help_text="Omdömen och betyg från profilen syns inte förrän någon intygat att den är kundens.",
                verbose_name="Profilen liknar inte företaget",
            ),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="google_place_confirmed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Profilen intygad"),
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="google_place_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Profilen intygad av",
            ),
        ),
    ]
