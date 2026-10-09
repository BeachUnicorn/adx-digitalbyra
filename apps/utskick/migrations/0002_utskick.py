"""
Flamingo 2.0, steg S2 (apps/utskick/README.md, B.2 och J S2): utskicken,
mottagarna, de godkända länkvärdarna, de spårade länkarna, sms-koderna,
klicken, svarstrådarna och de inkommande meddelandena, plus utskick och
mottagare på samtyckesloggen, spärrlistan och händelserna (nullbara, B.0).

Ordningen (B.7): utskick.0001 och flamingo.0016 före, flamingo.0017
(förfrågans utskick) efter. Ingen e-postkolumn på Utskick än: de kommer med
S3 och db_default. Recipient.ses_message_id och opened_at finns redan nu,
så att S3 inte behöver lägga till kolumner på en tabell S2 skriver.

Sist får varje ny främmande nyckel sin raderingsregel också i databasen
(dbfk.py): den förra versionen känner inte till de här tabellerna, och när
den tar bort en kontakt, en förfrågan, ett konto eller en användare som en
rad här pekar på sköter databasen CASCADE och SET NULL.
"""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from apps.utskick import dbfk

#: Varje främmande nyckel som migreringen skapar (vakten i
#: test_s2_foundation.DbOnDeleteTests kontrollerar att ingen saknas).
S2_FOREIGN_KEYS = [
    ("utskick", "inboundmessage", "account"),
    ("utskick", "inboundmessage", "contact"),
    ("utskick", "recipient", "contact"),
    ("utskick", "recipient", "sms_message"),
    ("utskick", "recipient", "utskick"),
    ("utskick", "event", "recipient"),
    ("utskick", "event", "utskick"),
    ("utskick", "thread", "account"),
    ("utskick", "thread", "contact"),
    ("utskick", "thread", "lead"),
    ("utskick", "thread", "utskick"),
    ("utskick", "threadmessage", "inbound"),
    ("utskick", "threadmessage", "sent_by"),
    ("utskick", "threadmessage", "sms_message"),
    ("utskick", "threadmessage", "thread"),
    ("utskick", "trackedlink", "account"),
    ("utskick", "trackedlink", "campaign"),
    ("utskick", "trackedlink", "utskick"),
    ("utskick", "linkcode", "account"),
    ("utskick", "linkcode", "contact"),
    ("utskick", "linkcode", "recipient"),
    ("utskick", "linkcode", "link"),
    ("utskick", "utskick", "account"),
    ("utskick", "utskick", "confirmed_by"),
    ("utskick", "utskick", "created_by"),
    ("utskick", "click", "account"),
    ("utskick", "click", "contact"),
    ("utskick", "click", "recipient"),
    ("utskick", "click", "link"),
    ("utskick", "click", "utskick"),
    ("utskick", "consentlog", "utskick"),
    ("utskick", "suppression", "utskick"),
    ("utskick", "allowedhost", "account"),
    ("utskick", "allowedhost", "decided_by"),
    ("utskick", "allowedhost", "requested_by"),
]


def on_delete_in_database(apps, schema_editor):
    dbfk.apply(apps, schema_editor, S2_FOREIGN_KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0016_forfragans_kontakt"),
        ("sms", "0005_extra_avsandare"),
        ("utskick", "0001_kontakter"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InboundMessage",
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
                ("provider_id", models.CharField(max_length=120)),
                ("from_address", models.CharField(blank=True, max_length=254)),
                ("to_address", models.CharField(blank=True, max_length=254)),
                ("body", models.TextField(blank=True)),
                ("subject", models.CharField(blank=True, max_length=200)),
                ("received_at", models.DateTimeField()),
                ("routed_via", models.CharField(blank=True, max_length=30)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Väntar"),
                            ("routed", "Routat"),
                            ("ambiguous", "Flera kunder möjliga"),
                            ("unroutable", "Ingen kund"),
                            ("stop", "STOPP"),
                            ("start", "START"),
                            ("autoreply", "Autosvar"),
                            ("spam", "Skräp"),
                            ("ignored", "Ignorerat"),
                            ("counted", "Bara räknat"),
                        ],
                        default="pending",
                        max_length=10,
                    ),
                ),
                ("meta", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "contact",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="utskick.contact",
                    ),
                ),
            ],
            options={
                "verbose_name": "Inkommande meddelande",
                "verbose_name_plural": "Inkommande meddelanden",
            },
        ),
        migrations.CreateModel(
            name="Recipient",
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
                ("address", models.CharField(blank=True, max_length=254)),
                ("merge", models.JSONField(blank=True, default=dict)),
                ("basis", models.CharField(blank=True, max_length=17)),
                ("tracking_ok", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "I kö"),
                            ("skipped", "Hoppades över"),
                            ("sending", "Skickas"),
                            ("sent", "Skickat"),
                            ("delivered", "Levererat"),
                            ("failed", "Misslyckades"),
                            ("bounced", "Studsade"),
                            ("complained", "Klagomål"),
                            ("unknown", "Oklart läge"),
                            ("cancelled", "Avbrutet"),
                        ],
                        default="queued",
                        max_length=10,
                    ),
                ),
                (
                    "skip_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("no_consent", "Inget samtycke"),
                            ("declined", "Vill inte ha erbjudanden"),
                            ("pending_doi", "Väntar på bekräftelse"),
                            ("suppressed", "Avregistrerad"),
                            ("bounced", "Studsad adress"),
                            ("weekly_cap", "Veckotaket"),
                            ("no_address", "Saknar nummer eller e-post"),
                            ("invalid_number", "Ogiltigt nummer"),
                            ("country", "Land som inte är tillåtet"),
                            ("duplicate", "Dubblett"),
                            ("deleted", "Borttagen"),
                            ("address_changed", "Nytt nummer sedan utskicket skapades"),
                            ("reply_collision", "Fick nyss sms från en annan ADX-kund"),
                            ("recent", "Fick ett utskick nyligen"),
                            ("ses_suppressed", "Spärrad hos e-posttjänsten"),
                            ("adx_cap", "Taket för ADX-domänen"),
                        ],
                        max_length=16,
                    ),
                ),
                ("not_before", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("sms_sender", models.CharField(blank=True, max_length=16)),
                ("parts", models.PositiveSmallIntegerField(default=0)),
                ("ses_message_id", models.CharField(blank=True, max_length=100)),
                ("error", models.CharField(blank=True, max_length=200)),
                ("first_clicked_at", models.DateTimeField(blank=True, null=True)),
                ("click_count", models.PositiveSmallIntegerField(default=0)),
                ("bot_hits", models.PositiveSmallIntegerField(default=0)),
                ("opened_at", models.DateTimeField(blank=True, null=True)),
                ("replied_at", models.DateTimeField(blank=True, null=True)),
                ("stopped_at", models.DateTimeField(blank=True, null=True)),
                ("simulated", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "contact",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="recipients",
                        to="utskick.contact",
                    ),
                ),
                (
                    "sms_message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="utskick_recipients",
                        to="sms.smsmessage",
                    ),
                ),
            ],
            options={
                "verbose_name": "Mottagare",
                "verbose_name_plural": "Mottagare",
            },
        ),
        migrations.AddField(
            model_name="event",
            name="recipient",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="utskick.recipient",
            ),
        ),
        migrations.CreateModel(
            name="Thread",
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
                    "kind",
                    models.CharField(
                        choices=[("reply", "Svar"), ("stop", "STOPP"), ("direct", "Direkt")],
                        default="reply",
                        max_length=6,
                    ),
                ),
                ("address", models.CharField(blank=True, max_length=254)),
                ("unread", models.BooleanField(default=True)),
                ("looks_like_stop", models.BooleanField(default=False)),
                ("last_in_at", models.DateTimeField(blank=True, null=True)),
                ("last_out_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_threads",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "contact",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="threads",
                        to="utskick.contact",
                    ),
                ),
                (
                    "lead",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reply_thread",
                        to="flamingo.lead",
                    ),
                ),
            ],
            options={
                "verbose_name": "Svarstråd",
                "verbose_name_plural": "Svarstrådar",
            },
        ),
        migrations.CreateModel(
            name="ThreadMessage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "direction",
                    models.CharField(choices=[("in", "In"), ("out", "Ut")], max_length=3),
                ),
                ("body", models.TextField()),
                ("subject", models.CharField(blank=True, max_length=200)),
                ("at", models.DateTimeField(default=django.utils.timezone.now)),
                ("email_message_id", models.CharField(blank=True, max_length=200)),
                ("attachments", models.JSONField(blank=True, default=list)),
                ("sent_as_staff", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("received", "Mottaget"),
                            ("sending", "Skickas"),
                            ("sent", "Skickat"),
                            ("failed", "Misslyckades"),
                        ],
                        default="received",
                        max_length=8,
                    ),
                ),
                (
                    "inbound",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="utskick.inboundmessage",
                    ),
                ),
                (
                    "sent_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "sms_message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="thread_messages",
                        to="sms.smsmessage",
                    ),
                ),
                (
                    "thread",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="utskick.thread",
                    ),
                ),
            ],
            options={
                "verbose_name": "Meddelande i tråd",
                "verbose_name_plural": "Meddelanden i trådar",
                "ordering": ["at", "pk"],
            },
        ),
        migrations.CreateModel(
            name="TrackedLink",
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
                        choices=[
                            ("lp", "Flamingo-sida"),
                            ("external", "Extern"),
                            ("named", "Namngiven"),
                        ],
                        max_length=8,
                    ),
                ),
                ("key", models.CharField(blank=True, max_length=40)),
                ("destination", models.CharField(max_length=500)),
                ("label", models.CharField(blank=True, max_length=120)),
                ("add_utm", models.BooleanField(default=True)),
                ("block_id", models.CharField(blank=True, max_length=16)),
                ("position", models.PositiveSmallIntegerField(default=0)),
                ("slug", models.SlugField(blank=True, max_length=40)),
                ("human_clicks", models.PositiveIntegerField(default=0)),
                ("bot_hits", models.PositiveIntegerField(default=0)),
                ("leads", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_links",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "campaign",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="utskick_links",
                        to="flamingo.campaign",
                    ),
                ),
            ],
            options={
                "verbose_name": "Spårad länk",
                "verbose_name_plural": "Spårade länkar",
            },
        ),
        migrations.CreateModel(
            name="LinkCode",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("code", models.CharField(max_length=8, unique=True)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("link", "Klicklänk"),
                            ("person", "Avregistrering och val (/s/, /p/)"),
                            ("confirm", "Bekräftelse (/b/)"),
                        ],
                        max_length=8,
                    ),
                ),
                ("channel", models.CharField(default="sms", max_length=5)),
                ("value_hash", models.CharField(max_length=64)),
                (
                    "purpose",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("signup", "Anmälan"),
                            ("start", "START"),
                            ("pref_on", "Slå på sms"),
                        ],
                        max_length=12,
                    ),
                ),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
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
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="utskick.contact",
                    ),
                ),
                (
                    "recipient",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="codes",
                        to="utskick.recipient",
                    ),
                ),
                (
                    "link",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="codes",
                        to="utskick.trackedlink",
                    ),
                ),
            ],
            options={
                "verbose_name": "Sms-kod",
                "verbose_name_plural": "Sms-koder",
            },
        ),
        migrations.CreateModel(
            name="Utskick",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=120, verbose_name="Namn")),
                (
                    "purpose",
                    models.CharField(
                        choices=[("reklam", "Reklam"), ("information", "Information")],
                        default="reklam",
                        max_length=12,
                        verbose_name="Syfte",
                    ),
                ),
                (
                    "info_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("bokning", "Bokning"),
                            ("arende", "Ärende"),
                            ("oppettider", "Ändrade öppettider"),
                            ("driftstorning", "Driftstörning"),
                            ("annat", "Annat"),
                        ],
                        max_length=14,
                        verbose_name="Skäl för information",
                    ),
                ),
                (
                    "info_reason_text",
                    models.CharField(blank=True, max_length=200, verbose_name="Annat skäl"),
                ),
                ("content_override", models.JSONField(blank=True, default=dict)),
                (
                    "channel_mode",
                    models.CharField(
                        choices=[
                            ("sms_only", "Bara sms"),
                            ("email_only", "Bara e-post"),
                            ("sms_then_email", "Sms, annars e-post"),
                            ("both", "Både sms och e-post"),
                        ],
                        default="sms_only",
                        max_length=16,
                        verbose_name="Kanal",
                    ),
                ),
                ("audience", models.JSONField(blank=True, default=dict, verbose_name="Mottagare")),
                ("sms_body", models.TextField(blank=True, verbose_name="Sms-text")),
                (
                    "sms_sender_kind",
                    models.CharField(
                        choices=[("reply", "Svarsnumret"), ("name", "Avsändarnamn")],
                        default="reply",
                        max_length=6,
                        verbose_name="Avsändare",
                    ),
                ),
                (
                    "sms_sender_name",
                    models.CharField(blank=True, max_length=11, verbose_name="Avsändarnamn"),
                ),
                ("merge_fallbacks", models.JSONField(blank=True, default=dict)),
                (
                    "send_mode",
                    models.CharField(
                        choices=[("now", "Nu"), ("at", "Vid en tid")], default="at", max_length=5
                    ),
                ),
                (
                    "scheduled_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Skickas"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Utkast"),
                            ("scheduled", "Schemalagt"),
                            ("freezing", "Förbereds"),
                            ("sending", "Skickas"),
                            ("paused_cap", "Pausat vid taket"),
                            ("paused_health", "Pausat"),
                            ("paused", "Pausat"),
                            ("sent", "Skickat"),
                            ("cancelled", "Avbrutet"),
                        ],
                        default="draft",
                        max_length=14,
                    ),
                ),
                (
                    "pause_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("sms_cost_cap", "Pausat vid taket"),
                            ("adx_mail_cap", "Pausat vid taket"),
                            ("bounces", "Pausat: studsar"),
                            ("complaints", "Pausat: klagomål"),
                            ("stops", "Pausat: avregistreringar"),
                            ("account_health", "Pausat: studsar"),
                            ("audience_grew", "Pausat"),
                            ("late", "Pausat"),
                            ("sms_disabled", "Pausat"),
                            ("email_disabled", "Pausat"),
                            ("provider", "Pausat"),
                            ("account_disabled", "Pausat"),
                            ("blocked", "Pausat"),
                            ("staff", "Pausat av ADX"),
                            ("customer", "Pausat"),
                            ("content", "Pausat"),
                        ],
                        default="",
                        max_length=20,
                    ),
                ),
                ("hold_until", models.DateTimeField(blank=True, null=True)),
                ("status_changed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("confirm_nonce", models.CharField(blank=True, max_length=32)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("confirmed_as_staff", models.BooleanField(default=False)),
                ("confirm_summary", models.JSONField(blank=True, default=dict)),
                ("freeze_cursor", models.PositiveBigIntegerField(default=0)),
                ("frozen_at", models.DateTimeField(blank=True, null=True)),
                ("frozen_counts", models.JSONField(blank=True, default=dict)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("stats", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_set",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "confirmed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
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
                "verbose_name": "Utskick",
                "verbose_name_plural": "Utskick",
                "ordering": ["-created_at", "-pk"],
            },
        ),
        migrations.AddField(
            model_name="trackedlink",
            name="utskick",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="links",
                to="utskick.utskick",
            ),
        ),
        migrations.AddField(
            model_name="thread",
            name="utskick",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="threads",
                to="utskick.utskick",
            ),
        ),
        migrations.AddField(
            model_name="recipient",
            name="utskick",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="recipients",
                to="utskick.utskick",
            ),
        ),
        migrations.CreateModel(
            name="Click",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[("sms", "Sms"), ("email", "E-post"), ("named", "Namngiven länk")],
                        max_length=5,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("human", "Människa"), ("scanner", "Skanner")],
                        default="human",
                        max_length=8,
                    ),
                ),
                ("at", models.DateTimeField(default=django.utils.timezone.now)),
                ("repeat_count", models.PositiveSmallIntegerField(default=0)),
                ("device", models.CharField(blank=True, max_length=8)),
                ("os", models.CharField(blank=True, max_length=20)),
                ("browser", models.CharField(blank=True, max_length=20)),
                ("ip_hash", models.CharField(blank=True, max_length=64)),
                ("lp_visits", models.PositiveSmallIntegerField(default=0)),
                ("first_visit_at", models.DateTimeField(blank=True, null=True)),
                ("engaged_seconds", models.PositiveSmallIntegerField(default=0)),
                ("beacon_at", models.DateTimeField(blank=True, null=True)),
                ("called", models.BooleanField(default=False)),
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
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="clicks",
                        to="utskick.contact",
                    ),
                ),
                (
                    "recipient",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="clicks",
                        to="utskick.recipient",
                    ),
                ),
                (
                    "link",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="clicks",
                        to="utskick.trackedlink",
                    ),
                ),
                (
                    "utskick",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="clicks",
                        to="utskick.utskick",
                    ),
                ),
            ],
            options={
                "verbose_name": "Klick",
                "verbose_name_plural": "Klick",
            },
        ),
        migrations.AddField(
            model_name="consentlog",
            name="utskick",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="utskick.utskick",
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="utskick",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="utskick.utskick",
            ),
        ),
        migrations.AddField(
            model_name="suppression",
            name="utskick",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="utskick.utskick",
            ),
        ),
        migrations.CreateModel(
            name="AllowedHost",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("host", models.CharField(max_length=253)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Väntar på ADX"),
                            ("approved", "Godkänd"),
                            ("refused", "Nekad"),
                        ],
                        default="pending",
                        max_length=8,
                    ),
                ),
                ("requested_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.CharField(blank=True, max_length=200)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utskick_hosts",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "requested_by",
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
                "verbose_name": "Godkänd länkvärd",
                "verbose_name_plural": "Godkända länkvärdar",
                "constraints": [
                    models.UniqueConstraint(fields=("account", "host"), name="utskick_host_unique")
                ],
            },
        ),
        migrations.AddIndex(
            model_name="inboundmessage",
            index=models.Index(fields=["status", "-received_at"], name="utskick_inbound_status"),
        ),
        migrations.AddIndex(
            model_name="inboundmessage",
            index=models.Index(
                fields=["from_address", "-received_at"], name="utskick_inbound_from"
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundmessage",
            constraint=models.UniqueConstraint(
                fields=("channel", "provider_id"), name="utskick_inbound_unique"
            ),
        ),
        migrations.AddIndex(
            model_name="threadmessage",
            index=models.Index(fields=["thread", "at"], name="utskick_tmsg_thread"),
        ),
        migrations.AddIndex(
            model_name="linkcode",
            index=models.Index(fields=["kind", "created_at"], name="utskick_code_kind"),
        ),
        migrations.AddConstraint(
            model_name="linkcode",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "link")),
                fields=("recipient", "link"),
                name="utskick_code_link",
            ),
        ),
        migrations.AddIndex(
            model_name="utskick",
            index=models.Index(
                fields=["account", "status", "-created_at"], name="utskick_utskick_status"
            ),
        ),
        migrations.AddIndex(
            model_name="utskick",
            index=models.Index(fields=["status", "scheduled_at"], name="utskick_utskick_due"),
        ),
        migrations.AddConstraint(
            model_name="trackedlink",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("utskick__isnull", False), models.Q(("key", ""), _negated=True)
                ),
                fields=("utskick", "key"),
                name="utskick_link_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="trackedlink",
            constraint=models.UniqueConstraint(
                condition=models.Q(("slug", ""), _negated=True),
                fields=("account", "slug"),
                name="utskick_link_slug",
            ),
        ),
        migrations.AddIndex(
            model_name="thread",
            index=models.Index(
                fields=["account", "contact", "channel", "-last_in_at"],
                name="utskick_thread_contact",
            ),
        ),
        migrations.AddIndex(
            model_name="recipient",
            index=models.Index(
                condition=models.Q(("status", "queued")),
                fields=["channel", "not_before", "id"],
                name="utskick_rcpt_queue",
            ),
        ),
        migrations.AddIndex(
            model_name="recipient",
            index=models.Index(fields=["utskick", "status"], name="utskick_rcpt_status"),
        ),
        migrations.AddIndex(
            model_name="recipient",
            index=models.Index(fields=["contact", "channel", "sent_at"], name="utskick_rcpt_week"),
        ),
        migrations.AddIndex(
            model_name="recipient",
            index=models.Index(
                condition=models.Q(("status", "sending")),
                fields=["status", "claimed_at"],
                name="utskick_rcpt_claimed",
            ),
        ),
        migrations.AddIndex(
            model_name="recipient",
            index=models.Index(
                condition=models.Q(("ses_message_id", ""), _negated=True),
                fields=["ses_message_id"],
                name="utskick_rcpt_ses_id",
            ),
        ),
        migrations.AddConstraint(
            model_name="recipient",
            constraint=models.UniqueConstraint(
                condition=models.Q(("contact__isnull", False)),
                fields=("utskick", "contact", "channel"),
                name="utskick_recipient_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="click",
            index=models.Index(fields=["utskick", "at"], name="utskick_click_utskick"),
        ),
        migrations.AddIndex(
            model_name="click",
            index=models.Index(fields=["recipient", "link", "at"], name="utskick_click_rcpt"),
        ),
        migrations.AddIndex(
            model_name="click",
            index=models.Index(fields=["account", "-at"], name="utskick_click_account"),
        ),
        migrations.RunPython(on_delete_in_database, migrations.RunPython.noop),
    ]
