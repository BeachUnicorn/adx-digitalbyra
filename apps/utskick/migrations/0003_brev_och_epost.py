"""
Flamingo 2.0, steg S3 (apps/utskick/README.md, B.3 och J S3): e-posten i
Brev, kundernas avsändardomäner, mejlens bilder och kvittona för
SES-händelserna ur SQS.

- Utskick får e-postens kolumner (B.2): ämnesrad, förhandstext, blocken,
  revisionen, accentfärgen, loggans plats, avsändardomänen och namnet, de
  bekräftade villkoren, pixeln, textversionen och mejlet som det frystes.
- UtskickSettings får hälsospärren för e-post (D.9) och Switchboard
  SES-kontots läge (GetAccount, utskick_daily).
- Nya tabeller: SenderDomain, EmailImage, EventReceipt.

B.0: S2-koden skriver Utskick, UtskickSettings och Switchboard mellan
migrate och reload, och för gott efter en tillbakarullning, så varje nytt
fält där är nullbart eller har db_default (vakten
test_s1_guards.MigrationRuleTests). Sist får varje ny främmande nyckel med
CASCADE eller SET_NULL sin regel också i databasen (dbfk.py, vakten
test_s2_foundation.DbOnDeleteTests): S2-koden känner inte till de nya
tabellerna när den tar bort ett konto, en bild eller en användare.
Utskick.sender_domain är RESTRICT och får ingen regel (NO ACTION, prövas vid
commit): S2-koden tar aldrig bort en domän, och kontots borttagning tar
utskicken först.
"""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import apps.utskick.models
from apps.utskick import dbfk

#: Varje främmande nyckel som migreringen skapar (vakten i
#: test_s2_foundation.DbOnDeleteTests kontrollerar att ingen saknas).
S3_FOREIGN_KEYS = [
    ("utskick", "utskick", "terms_confirmed_by"),
    ("utskick", "utskick", "sender_domain"),
    ("utskick", "utskicksettings", "email_released_by"),
    ("utskick", "senderdomain", "account"),
    ("utskick", "senderdomain", "created_by"),
    ("utskick", "emailimage", "account"),
    ("utskick", "emailimage", "asset"),
]


def on_delete_in_database(apps, schema_editor):
    dbfk.apply(apps, schema_editor, S3_FOREIGN_KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0017_svar_pa_utskick"),
        ("utskick", "0002_utskick"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="switchboard",
            name="ses_account",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, output_field=models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="switchboard",
            name="ses_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="utskick",
            name="accent",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=7, verbose_name="Accentfärg"
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="confirmed_terms",
            field=models.JSONField(
                blank=True,
                db_default=models.Value([], output_field=models.JSONField()),
                default=list,
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="email_doc",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, output_field=models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="email_rev",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="utskick",
            name="email_snapshot",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, output_field=models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="from_name",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=80, verbose_name="Avsändarnamn"
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="logo_position",
            field=models.CharField(
                choices=[("left", "Till vänster"), ("center", "I mitten"), ("none", "Ingen logga")],
                db_default="left",
                default="left",
                max_length=6,
                verbose_name="Logga",
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="open_tracking",
            field=models.BooleanField(
                db_default=False, default=False, verbose_name="Spåra öppningar"
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="preheader",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=150, verbose_name="Förhandstext"
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="subject",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=150, verbose_name="Ämnesrad"
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="terms_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="utskick",
            name="terms_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="utskick",
            name="text_override",
            field=models.TextField(
                blank=True, db_default="", default="", verbose_name="Textversion"
            ),
        ),
        migrations.AddField(
            model_name="utskicksettings",
            name="email_blocked_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="E-post spärrad (hälsan)"
            ),
        ),
        migrations.AddField(
            model_name="utskicksettings",
            name="email_blocked_reason",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=20,
                verbose_name="Varför e-posten är spärrad",
            ),
        ),
        migrations.AddField(
            model_name="utskicksettings",
            name="email_released_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Spärren släppt"),
        ),
        migrations.AddField(
            model_name="utskicksettings",
            name="email_released_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="EventReceipt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("key", models.CharField(max_length=140, unique=True)),
                ("at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "verbose_name": "Kvitto för SES-händelse",
                "verbose_name_plural": "Kvitton för SES-händelser",
                "indexes": [models.Index(fields=["at"], name="utskick_receipt_at")],
            },
        ),
        migrations.CreateModel(
            name="SenderDomain",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("domain", models.CharField(max_length=253, verbose_name="Domän")),
                (
                    "from_local",
                    models.CharField(
                        default="hej", max_length=64, verbose_name="Avsändaradressens första del"
                    ),
                ),
                ("from_name", models.CharField(max_length=80, verbose_name="Avsändarnamn")),
                ("mail_from_sub", models.CharField(default="studs", max_length=40)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Väntar på DNS"),
                            ("verified", "Verifierad"),
                            ("failed", "Misslyckades"),
                            ("expired", "Gick ut"),
                            ("removed", "Borttagen"),
                        ],
                        default="pending",
                        max_length=8,
                    ),
                ),
                ("ses_created", models.BooleanField(default=False)),
                ("dkim_tokens", models.JSONField(blank=True, default=list)),
                ("checks", models.JSONField(blank=True, default=dict)),
                ("ses_snapshot", models.JSONField(blank=True, default=dict)),
                ("checked_at", models.DateTimeField(blank=True, null=True)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("probe_passed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_domains",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Avsändardomän",
                "verbose_name_plural": "Avsändardomäner",
                "ordering": ["-created_at", "-pk"],
            },
        ),
        migrations.AddField(
            model_name="utskick",
            name="sender_domain",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="utskick_set",
                to="utskick.senderdomain",
            ),
        ),
        migrations.CreateModel(
            name="EmailImage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "purpose",
                    models.CharField(
                        choices=[
                            ("content", "Bild i mejlet"),
                            ("logo", "Logotyp"),
                            ("video", "Videobild med spelknapp"),
                            ("avatar", "Porträtt"),
                        ],
                        max_length=7,
                    ),
                ),
                (
                    "file",
                    models.ImageField(
                        max_length=200, upload_to=apps.utskick.models.email_image_path
                    ),
                ),
                (
                    "format",
                    models.CharField(choices=[("jpeg", "JPEG"), ("png", "PNG")], max_length=4),
                ),
                ("width", models.PositiveIntegerField(default=0)),
                ("height", models.PositiveIntegerField(default=0)),
                ("bytes", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_email_images",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "asset",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="email_images",
                        to="flamingo.mediaasset",
                    ),
                ),
            ],
            options={
                "verbose_name": "Bild i mejl",
                "verbose_name_plural": "Bilder i mejl",
                "indexes": [
                    models.Index(
                        fields=["account", "created_at"], name="utskick_emailimage_account"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("asset__isnull", False)),
                        fields=("asset", "purpose", "width"),
                        name="utskick_emailimage_rendition",
                    )
                ],
            },
        ),
        migrations.AddIndex(
            model_name="senderdomain",
            index=models.Index(fields=["account", "status"], name="utskick_domain_account"),
        ),
        migrations.AddIndex(
            model_name="senderdomain",
            index=models.Index(fields=["domain"], name="utskick_domain_name"),
        ),
        migrations.AddIndex(
            model_name="senderdomain",
            index=models.Index(fields=["status", "created_at"], name="utskick_domain_status"),
        ),
        migrations.AddConstraint(
            model_name="senderdomain",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "verified")),
                fields=("domain",),
                name="utskick_domain_verified",
            ),
        ),
        migrations.RunPython(on_delete_in_database, migrations.RunPython.noop),
    ]
