"""
Flamingo 2.0, steg S1 (apps/utskick/README.md, B.1): kontaktregistret med
samtycke per kanal, samtyckesloggen, spärrlistan, extrafält, taggar och
listor, importerna, anmälningssidan, biträdesavtalet, händelserna,
exporterna, räknarna och byråns brytare.

Bara nya tabeller. Beror på flamingo.0015 och sms.0005 (ordningen i B.7);
flamingo.0016 (Lead.contact) beror i sin tur på den här.
"""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import apps.projects.models
import apps.utskick.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("flamingo", "0015_utvalda_omdomen_fran_reco"),
        ("sms", "0005_extra_avsandare"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Contact",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("person", "Privatperson"), ("company", "Företag")],
                        default="person",
                        max_length=10,
                        verbose_name="Typ",
                    ),
                ),
                ("first_name", models.CharField(blank=True, max_length=60, verbose_name="Förnamn")),
                (
                    "last_name",
                    models.CharField(blank=True, max_length=80, verbose_name="Efternamn"),
                ),
                (
                    "company_name",
                    models.CharField(blank=True, max_length=120, verbose_name="Företag"),
                ),
                (
                    "org_number",
                    models.CharField(blank=True, max_length=10, verbose_name="Organisationsnummer"),
                ),
                ("phone", models.CharField(blank=True, max_length=16, verbose_name="Mobil")),
                ("phone_country", models.CharField(blank=True, max_length=2)),
                ("email", models.CharField(blank=True, max_length=254, verbose_name="E-post")),
                (
                    "email_state",
                    models.CharField(
                        choices=[("ok", "Fungerar"), ("bounced", "Studsad")],
                        default="ok",
                        max_length=10,
                    ),
                ),
                ("email_soft_bounces", models.PositiveSmallIntegerField(default=0)),
                ("email_bounced_at", models.DateTimeField(blank=True, null=True)),
                ("fields", models.JSONField(blank=True, default=dict, verbose_name="Extrafält")),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("import", "Import"),
                            ("form", "Formulär"),
                            ("signup", "Anmälan"),
                            ("manual", "Manuellt"),
                            ("api", "API"),
                            ("reply", "Svar"),
                            ("lead", "Förfrågan"),
                        ],
                        max_length=10,
                        verbose_name="Källa",
                    ),
                ),
                ("source_detail", models.CharField(blank=True, max_length=200)),
                ("search_text", models.TextField(blank=True)),
                ("last_activity_at", models.DateTimeField(blank=True, null=True)),
                ("last_activity_kind", models.CharField(blank=True, max_length=20)),
                ("inactive_flagged_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_contacts",
                        to="flamingo.flamingoaccount",
                    ),
                ),
            ],
            options={
                "verbose_name": "Kontakt",
                "verbose_name_plural": "Kontakter",
                "ordering": ["-created_at", "-pk"],
            },
        ),
        migrations.CreateModel(
            name="ContactList",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=80, verbose_name="Namn")),
                (
                    "description",
                    models.CharField(blank=True, max_length=200, verbose_name="Beskrivning"),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_lists",
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
                "verbose_name": "Lista",
                "verbose_name_plural": "Listor",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Counter",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("scope", models.CharField(max_length=20)),
                ("key", models.CharField(blank=True, max_length=80)),
                ("window", models.DateTimeField()),
                ("count", models.PositiveIntegerField(default=0)),
            ],
            options={
                "verbose_name": "Räknare",
                "verbose_name_plural": "Räknare",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("scope", "key", "window"), name="utskick_counter_window"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="DpaVersion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("version", models.CharField(max_length=20, unique=True, verbose_name="Version")),
                ("text", models.TextField(verbose_name="Text")),
                ("sha256", models.CharField(max_length=64)),
                ("published_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("is_current", models.BooleanField(default=False, verbose_name="Aktuell")),
                (
                    "published_by",
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
                "verbose_name": "Biträdesavtal",
                "verbose_name_plural": "Biträdesavtal",
                "ordering": ["-published_at", "-pk"],
            },
        ),
        migrations.CreateModel(
            name="DpaAcceptance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("accepted_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("accepted_as_staff", models.BooleanField(default=False)),
                (
                    "staff_statement",
                    models.CharField(
                        blank=True, max_length=300, verbose_name="Godkänt av, och hur"
                    ),
                ),
                ("ip_hash", models.CharField(blank=True, max_length=64)),
                (
                    "accepted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_dpa",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="acceptances",
                        to="utskick.dpaversion",
                    ),
                ),
            ],
            options={
                "verbose_name": "Godkänt biträdesavtal",
                "verbose_name_plural": "Godkända biträdesavtal",
            },
        ),
        migrations.CreateModel(
            name="Event",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("kind", models.CharField(max_length=20)),
                ("at", models.DateTimeField(default=django.utils.timezone.now)),
                ("data", models.JSONField(blank=True, default=dict)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "contact",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="utskick.contact",
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="flamingo.lead",
                    ),
                ),
            ],
            options={
                "verbose_name": "Händelse",
                "verbose_name_plural": "Händelser",
                "ordering": ["-at", "-pk"],
            },
        ),
        migrations.CreateModel(
            name="ExportLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("as_staff", models.BooleanField(default=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("contacts", "Hela registret"),
                            ("contact", "En person"),
                            ("utskick", "Mottagare"),
                        ],
                        max_length=12,
                    ),
                ),
                ("rows", models.PositiveIntegerField(default=0)),
                ("at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "user",
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
                "verbose_name": "Export",
                "verbose_name_plural": "Exporter",
                "ordering": ["-at", "-pk"],
            },
        ),
        migrations.CreateModel(
            name="FieldDef",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("key", models.SlugField(max_length=40, verbose_name="Nyckel")),
                ("label", models.CharField(max_length=60, verbose_name="Rubrik")),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("text", "Text"),
                            ("date", "Datum"),
                            ("number", "Tal"),
                            ("choice", "Val"),
                        ],
                        default="text",
                        max_length=8,
                        verbose_name="Typ",
                    ),
                ),
                ("choices", models.JSONField(blank=True, default=list, verbose_name="Val")),
                ("order", models.PositiveSmallIntegerField(default=0)),
                ("show_in_list", models.BooleanField(default=False, verbose_name="Visas i listan")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_fields",
                        to="flamingo.flamingoaccount",
                    ),
                ),
            ],
            options={
                "verbose_name": "Extrafält",
                "verbose_name_plural": "Extrafält",
                "ordering": ["order", "pk"],
            },
        ),
        migrations.CreateModel(
            name="ListMembership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("added_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("import", "Import"),
                            ("manual", "Manuellt"),
                            ("signup", "Anmälan"),
                            ("flow", "Flöde"),
                            ("api", "API"),
                            ("report", "Rapport"),
                        ],
                        default="manual",
                        max_length=10,
                    ),
                ),
                (
                    "contact",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="utskick.contact",
                    ),
                ),
                (
                    "list",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="utskick.contactlist",
                    ),
                ),
            ],
            options={
                "verbose_name": "Plats i lista",
                "verbose_name_plural": "Platser i listor",
            },
        ),
        migrations.CreateModel(
            name="Suppression",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "channel",
                    models.CharField(choices=[("sms", "Sms"), ("email", "E-post")], max_length=5),
                ),
                ("value_hash", models.CharField(max_length=64)),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("stop", "STOPP"),
                            ("link", "Avregistreringslänk"),
                            ("list_unsub", "Avregistrering i e-postprogrammet"),
                            ("preference", "Mina utskick"),
                            ("bounce", "Studsad adress"),
                            ("complaint", "Klagomål"),
                            ("manual", "Manuellt"),
                            ("import", "Import"),
                            ("erasure", "Borttagen kontakt"),
                            ("reply", "Svar"),
                        ],
                        max_length=12,
                    ),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("note", models.CharField(blank=True, max_length=200)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_suppressions",
                        to="flamingo.flamingoaccount",
                    ),
                ),
            ],
            options={
                "verbose_name": "Spärr",
                "verbose_name_plural": "Spärrlistan",
            },
        ),
        migrations.CreateModel(
            name="Switchboard",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("sms_enabled", models.BooleanField(default=False, verbose_name="Sms-utskick på")),
                (
                    "email_enabled",
                    models.BooleanField(default=False, verbose_name="E-postutskick på"),
                ),
                (
                    "sms_paused_until",
                    models.DateTimeField(blank=True, null=True, verbose_name="Sms pausade till"),
                ),
                (
                    "doi_ready_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Bekräftelsemejl klara"
                    ),
                ),
                (
                    "links_ready_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Länkvärdarna klara"),
                ),
                (
                    "sms_inbound_ready_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Inkommande sms klara"
                    ),
                ),
                (
                    "email_ready_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="E-post klar"),
                ),
                (
                    "ses_max_rate",
                    models.PositiveSmallIntegerField(default=0, verbose_name="SES högsta takt"),
                ),
                (
                    "ses_daily_quota",
                    models.PositiveIntegerField(default=0, verbose_name="SES dygnskvot"),
                ),
                ("hash_fingerprint", models.CharField(blank=True, max_length=64)),
                ("link_fingerprint", models.CharField(blank=True, max_length=64)),
                ("last_tick_at", models.DateTimeField(blank=True, null=True)),
                ("last_tick_summary", models.JSONField(blank=True, default=dict)),
                ("last_queue_poll_at", models.DateTimeField(blank=True, null=True)),
                ("last_elks_reconcile_at", models.DateTimeField(blank=True, null=True)),
                ("changed_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.CharField(blank=True, max_length=200, verbose_name="Anteckning")),
                (
                    "changed_by",
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
                "verbose_name": "Nödstopp och klarmarkeringar",
                "verbose_name_plural": "Nödstopp och klarmarkeringar",
            },
        ),
        migrations.CreateModel(
            name="Tag",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=40, verbose_name="Namn")),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_tags",
                        to="flamingo.flamingoaccount",
                    ),
                ),
            ],
            options={
                "verbose_name": "Tagg",
                "verbose_name_plural": "Taggar",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="SignupForm",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("title", models.CharField(max_length=80, verbose_name="Rubrik")),
                ("intro", models.CharField(blank=True, max_length=400, verbose_name="Ingress")),
                (
                    "channels",
                    models.JSONField(
                        default=apps.utskick.models.default_signup_channels, verbose_name="Kanaler"
                    ),
                ),
                ("is_active", models.BooleanField(default=False, verbose_name="Sidan är på")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_signup",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "add_to_list",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="utskick.contactlist",
                    ),
                ),
                (
                    "add_tags",
                    models.ManyToManyField(blank=True, related_name="+", to="utskick.tag"),
                ),
            ],
            options={
                "verbose_name": "Anmälningssida",
                "verbose_name_plural": "Anmälningssidor",
            },
        ),
        migrations.CreateModel(
            name="ImportJob",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_as_staff", models.BooleanField(default=False)),
                (
                    "file",
                    models.FileField(
                        blank=True,
                        storage=apps.projects.models.private_storage,
                        upload_to="utskick-import/%Y/%m/",
                    ),
                ),
                ("csv_path", models.CharField(blank=True, max_length=300)),
                ("original_name", models.CharField(max_length=200)),
                (
                    "kind",
                    models.CharField(
                        choices=[("csv", "CSV"), ("xlsx", "Excel"), ("paste", "Inklistrat")],
                        max_length=5,
                    ),
                ),
                ("size", models.PositiveIntegerField(default=0)),
                ("delimiter", models.CharField(blank=True, max_length=1)),
                ("encoding", models.CharField(blank=True, max_length=20)),
                ("header", models.JSONField(blank=True, default=list)),
                ("sample", models.JSONField(blank=True, default=list)),
                ("row_count", models.PositiveIntegerField(default=0)),
                ("mapping", models.JSONField(blank=True, default=dict)),
                ("consent", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("uploaded", "Uppladdad"),
                            ("converting", "Läses in"),
                            ("mapping", "Kolumner"),
                            ("consent", "Samtycke"),
                            ("analysing", "Granskas"),
                            ("review", "Granska"),
                            ("importing", "Importeras"),
                            ("done", "Klar"),
                            ("failed", "Misslyckades"),
                            ("cancelled", "Avbruten"),
                        ],
                        default="uploaded",
                        max_length=10,
                    ),
                ),
                ("in_request", models.BooleanField(default=False)),
                ("byte_offset", models.PositiveBigIntegerField(default=0)),
                ("progress", models.PositiveIntegerField(default=0)),
                ("counts", models.JSONField(blank=True, default=dict)),
                ("errors", models.JSONField(blank=True, default=list)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("file_deleted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_imports",
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
                (
                    "target_list",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="utskick.contactlist",
                    ),
                ),
                (
                    "target_tag",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="utskick.tag",
                    ),
                ),
            ],
            options={
                "verbose_name": "Import",
                "verbose_name_plural": "Importer",
                "ordering": ["-created_at", "-pk"],
            },
        ),
        migrations.AddField(
            model_name="contact",
            name="tags",
            field=models.ManyToManyField(blank=True, related_name="contacts", to="utskick.tag"),
        ),
        migrations.CreateModel(
            name="UtskickSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "is_enabled",
                    models.BooleanField(default=False, verbose_name="Utskick aktiverat"),
                ),
                ("enabled_at", models.DateTimeField(blank=True, null=True)),
                ("disabled_at", models.DateTimeField(blank=True, null=True)),
                (
                    "public_slug",
                    models.SlugField(max_length=40, unique=True, verbose_name="Adress för anmälan"),
                ),
                (
                    "display_name",
                    models.CharField(max_length=80, verbose_name="Företagsnamn i utskick"),
                ),
                (
                    "consent_text_sms",
                    models.CharField(max_length=200, verbose_name="Samtyckestext för sms"),
                ),
                (
                    "consent_text_email",
                    models.CharField(max_length=200, verbose_name="Samtyckestext för e-post"),
                ),
                (
                    "lp_consent",
                    models.BooleanField(default=True, verbose_name="Kryssrutor på landningssidor"),
                ),
                (
                    "privacy_url",
                    models.URLField(blank=True, max_length=500, verbose_name="Integritetspolicy"),
                ),
                (
                    "pref_email_note",
                    models.CharField(
                        blank=True, max_length=120, verbose_name="Text under E-post med erbjudanden"
                    ),
                ),
                (
                    "sms_window",
                    models.JSONField(
                        default=apps.utskick.models.default_sms_window,
                        verbose_name="Tidsfönster för sms",
                    ),
                ),
                (
                    "weekly_cap_sms",
                    models.PositiveSmallIntegerField(
                        default=2, verbose_name="Sms per kontakt och vecka"
                    ),
                ),
                (
                    "weekly_cap_email",
                    models.PositiveSmallIntegerField(
                        default=4, verbose_name="Mejl per kontakt och vecka"
                    ),
                ),
                (
                    "open_tracking",
                    models.BooleanField(default=False, verbose_name="Spåra öppningar"),
                ),
                (
                    "email_reply_mode",
                    models.CharField(
                        choices=[("inbox", "Inkorgen"), ("own", "Egen adress")],
                        default="inbox",
                        max_length=8,
                        verbose_name="Svar på mejl",
                    ),
                ),
                (
                    "own_reply_to",
                    models.EmailField(blank=True, max_length=254, verbose_name="Egen svarsadress"),
                ),
                ("own_reply_to_confirmed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "unsubscribe_text",
                    models.CharField(
                        blank=True,
                        max_length=300,
                        verbose_name="Extra text på avregistreringssidan",
                    ),
                ),
                (
                    "contact_limit",
                    models.PositiveIntegerField(
                        default=25000, verbose_name="Högsta antal kontakter"
                    ),
                ),
                (
                    "notify_on_reply",
                    models.BooleanField(default=True, verbose_name="Meddela mig om svar"),
                ),
                ("reply_notice_at", models.DateTimeField(blank=True, null=True)),
                (
                    "sending_blocked",
                    models.BooleanField(default=False, verbose_name="All sändning stoppad av ADX"),
                ),
                (
                    "blocked_reason",
                    models.CharField(
                        blank=True, max_length=200, verbose_name="Varför sändningen är stoppad"
                    ),
                ),
                (
                    "email_daily_cap",
                    models.PositiveIntegerField(
                        default=0, verbose_name="Mejl per dag (0 = upptrappning)"
                    ),
                ),
                ("email_first_sent_at", models.DateTimeField(blank=True, null=True)),
                ("email_probe_passed_at", models.DateTimeField(blank=True, null=True)),
                ("first_utskick_alerted", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick",
                        to="flamingo.flamingoaccount",
                        verbose_name="Konto",
                    ),
                ),
                (
                    "enabled_by",
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
                "verbose_name": "Utskick för kund",
                "verbose_name_plural": "Utskick för kunder",
            },
        ),
        migrations.CreateModel(
            name="ConsentLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("channel", models.CharField(max_length=5)),
                ("value_hash", models.CharField(max_length=64)),
                ("old_status", models.CharField(blank=True, max_length=12)),
                ("new_status", models.CharField(max_length=12)),
                ("basis", models.CharField(max_length=17)),
                ("text_shown", models.TextField(blank=True)),
                ("evidence", models.CharField(blank=True, max_length=300)),
                ("source", models.CharField(max_length=16)),
                ("source_detail", models.CharField(blank=True, max_length=200)),
                ("by_label", models.CharField(blank=True, max_length=120)),
                ("by_staff", models.BooleanField(default=False)),
                ("ip_hash", models.CharField(blank=True, max_length=64)),
                ("at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "by_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "contact",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="consent_log",
                        to="utskick.contact",
                    ),
                ),
            ],
            options={
                "verbose_name": "Samtyckeslogg",
                "verbose_name_plural": "Samtyckesloggen",
                "ordering": ["-at", "-pk"],
                "indexes": [
                    models.Index(fields=["contact", "-at"], name="utskick_clog_contact"),
                    models.Index(fields=["account", "value_hash"], name="utskick_clog_hash"),
                    models.Index(fields=["value_hash", "-at"], name="utskick_clog_hash_at"),
                ],
            },
        ),
        migrations.CreateModel(
            name="Consent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "channel",
                    models.CharField(choices=[("sms", "Sms"), ("email", "E-post")], max_length=5),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("yes", "Ja"),
                            ("existing", "Befintlig kund"),
                            ("company", "Företag"),
                            ("pending", "Väntar på bekräftelse"),
                            ("missing", "Inget samtycke"),
                            ("declined", "Vill inte ha erbjudanden"),
                            ("unsubscribed", "Avregistrerad"),
                        ],
                        default="missing",
                        max_length=12,
                    ),
                ),
                (
                    "basis",
                    models.CharField(
                        choices=[
                            ("consent", "Samtycke"),
                            ("existing_customer", "Befintlig kund"),
                            ("company", "Företag"),
                            ("none", "Ingen grund"),
                        ],
                        default="none",
                        max_length=17,
                    ),
                ),
                ("value_hash", models.CharField(blank=True, max_length=64)),
                ("text_shown", models.TextField(blank=True)),
                ("tracking_ok", models.BooleanField(default=False)),
                ("evidence", models.CharField(blank=True, max_length=300)),
                (
                    "source",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("import", "Import"),
                            ("lp_form", "Formulär på landningssidan"),
                            ("signup", "Anmälningssidan"),
                            ("preference", "Mina utskick"),
                            ("doi", "Bekräftelse via e-post"),
                            ("confirm", "Bekräftelse via sms"),
                            ("manual", "Manuellt"),
                            ("api", "API"),
                            ("stop", "STOPP"),
                            ("start", "START"),
                            ("link", "Avregistreringslänk"),
                            ("list_unsub", "Avregistrering i e-postprogrammet"),
                            ("complaint", "Klagomål"),
                            ("reply", "Svar"),
                            ("address", "Ny adress"),
                        ],
                        max_length=16,
                    ),
                ),
                ("source_detail", models.CharField(blank=True, max_length=200)),
                ("collected_at", models.DateTimeField(blank=True, null=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("confirm_sent_at", models.DateTimeField(blank=True, null=True)),
                ("confirm_count", models.PositiveSmallIntegerField(default=0)),
                ("changed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("changed_by_label", models.CharField(blank=True, max_length=120)),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "contact",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="consents",
                        to="utskick.contact",
                    ),
                ),
            ],
            options={
                "verbose_name": "Samtycke",
                "verbose_name_plural": "Samtycken",
                "indexes": [
                    models.Index(fields=["channel", "status"], name="utskick_consent_status"),
                    models.Index(
                        condition=models.Q(
                            ("confirm_sent_at__isnull", True), ("status", "pending")
                        ),
                        fields=["changed_at"],
                        name="utskick_consent_to_confirm",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("contact", "channel"), name="utskick_consent_channel"
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="contactlist",
            constraint=models.UniqueConstraint(
                fields=("account", "name"), name="utskick_list_name"
            ),
        ),
        migrations.AddConstraint(
            model_name="dpaversion",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_current", True)),
                fields=("is_current",),
                name="utskick_dpa_current",
            ),
        ),
        migrations.AddIndex(
            model_name="dpaacceptance",
            index=models.Index(fields=["account", "-accepted_at"], name="utskick_dpa_account"),
        ),
        migrations.AddIndex(
            model_name="event",
            index=models.Index(fields=["contact", "-at"], name="utskick_event_contact"),
        ),
        migrations.AddIndex(
            model_name="event",
            index=models.Index(fields=["account", "kind", "-at"], name="utskick_event_kind"),
        ),
        migrations.AddIndex(
            model_name="exportlog",
            index=models.Index(fields=["account", "-at"], name="utskick_export_account"),
        ),
        migrations.AddConstraint(
            model_name="fielddef",
            constraint=models.UniqueConstraint(fields=("account", "key"), name="utskick_field_key"),
        ),
        migrations.AddConstraint(
            model_name="fielddef",
            constraint=models.UniqueConstraint(
                condition=models.Q(("show_in_list", True)),
                fields=("account",),
                name="utskick_field_in_list",
            ),
        ),
        migrations.AddConstraint(
            model_name="listmembership",
            constraint=models.UniqueConstraint(
                fields=("list", "contact"), name="utskick_membership"
            ),
        ),
        migrations.AddConstraint(
            model_name="suppression",
            constraint=models.UniqueConstraint(
                fields=("account", "channel", "value_hash"), name="utskick_suppression_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="tag",
            constraint=models.UniqueConstraint(fields=("account", "name"), name="utskick_tag_name"),
        ),
        migrations.AddIndex(
            model_name="importjob",
            index=models.Index(fields=["status", "created_at"], name="utskick_import_status"),
        ),
        migrations.AddIndex(
            model_name="contact",
            index=models.Index(
                fields=["account", "-last_activity_at"], name="utskick_contact_activity"
            ),
        ),
        migrations.AddIndex(
            model_name="contact",
            index=models.Index(fields=["account", "-created_at"], name="utskick_contact_created"),
        ),
        migrations.AddIndex(
            model_name="contact",
            index=models.Index(fields=["account", "kind"], name="utskick_contact_kind"),
        ),
        migrations.AddConstraint(
            model_name="contact",
            constraint=models.UniqueConstraint(
                condition=models.Q(("phone", ""), _negated=True),
                fields=("account", "phone"),
                name="utskick_contact_phone",
            ),
        ),
        migrations.AddConstraint(
            model_name="contact",
            constraint=models.UniqueConstraint(
                condition=models.Q(("email", ""), _negated=True),
                fields=("account", "email"),
                name="utskick_contact_email",
            ),
        ),
    ]
