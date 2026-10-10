"""
Flamingo 2.0, steg S4 (apps/utskick/README.md, B.4 och J S4): segmenten och
skriptet på kundens egen webbplats.

- Nya tabeller: Segment (regler som segments.py tolkar, med senaste
  räkningen) och SiteSnippet (en domän och skriptets offentliga nyckel).
- TrackedLink: de namngivna länkarna (utskick null) får en regel, en länk
  utan utskick har alltid en slug, och ett delindex för listan i Länkar.
  Slug-kolumnen och dess unika villkor per konto finns sedan 0002 (S2).

B.0: inget nytt fält på en äldre tabell. Regeln och indexet på TrackedLink
stör inte S3-koden, som bara skapar länkar med utskick. Sist får varje ny
främmande nyckel med CASCADE eller SET_NULL sin regel också i databasen
(dbfk.py, vakten test_s2_foundation.DbOnDeleteTests): S3-koden känner inte
till de nya tabellerna när den tar bort ett konto eller en användare.
"""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import apps.utskick.models
from apps.utskick import dbfk

#: Varje främmande nyckel som migreringen skapar (vakten i
#: test_s4_foundation.DbOnDeleteTests kontrollerar att ingen saknas).
S4_FOREIGN_KEYS = [
    ("utskick", "segment", "account"),
    ("utskick", "segment", "created_by"),
    ("utskick", "sitesnippet", "account"),
]


def on_delete_in_database(apps, schema_editor):
    dbfk.apply(apps, schema_editor, S4_FOREIGN_KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0017_svar_pa_utskick"),
        ("utskick", "0003_brev_och_epost"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Segment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=80, verbose_name="Namn")),
                ("rules", models.JSONField(blank=True, default=dict)),
                ("cached_count", models.PositiveIntegerField(default=0)),
                ("cached_sms", models.PositiveIntegerField(default=0)),
                ("cached_email", models.PositiveIntegerField(default=0)),
                ("counted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Segment",
                "verbose_name_plural": "Segment",
                "ordering": ["name", "pk"],
            },
        ),
        migrations.CreateModel(
            name="SiteSnippet",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("domain", models.CharField(max_length=253, verbose_name="Domän")),
                (
                    "key",
                    models.CharField(
                        default=apps.utskick.models.new_snippet_key, max_length=16, unique=True
                    ),
                ),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "verbose_name": "Spårningsskript",
                "verbose_name_plural": "Spårningsskript",
                "ordering": ["domain", "pk"],
            },
        ),
        migrations.AddIndex(
            model_name="trackedlink",
            index=models.Index(
                condition=models.Q(("utskick__isnull", True)),
                fields=["account", "-created_at"],
                name="utskick_link_named",
            ),
        ),
        migrations.AddConstraint(
            model_name="trackedlink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("utskick__isnull", False),
                    models.Q(("slug", ""), _negated=True),
                    _connector="OR",
                ),
                name="utskick_link_named_slug",
            ),
        ),
        migrations.AddField(
            model_name="segment",
            name="account",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="utskick_segments",
                to="flamingo.flamingoaccount",
            ),
        ),
        migrations.AddField(
            model_name="segment",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="sitesnippet",
            name="account",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="utskick_sites",
                to="flamingo.flamingoaccount",
            ),
        ),
        migrations.AddConstraint(
            model_name="segment",
            constraint=models.UniqueConstraint(
                fields=("account", "name"), name="utskick_segment_name"
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesnippet",
            constraint=models.UniqueConstraint(
                fields=("account", "domain"), name="utskick_site_domain"
            ),
        ),
        migrations.RunPython(on_delete_in_database, migrations.RunPython.noop),
    ]
