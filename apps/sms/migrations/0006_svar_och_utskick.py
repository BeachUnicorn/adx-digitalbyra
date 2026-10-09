"""
Sms från Flamingos utskick (apps/utskick/README.md, B.7 och C.1):

- SmsMessage.source: api (som förut), utskick, flow, reply, system, test.
  db_default "api": den förra versionen skriver API:ts sms utan fältet
  (utskick B.0);
- SmsMessage.sender 11 -> 16 tecken: svarsnumret i E.164;
- index (konto, källa, tid) för portalens filter och underlagets källor, och
  ett partiellt index (to, tid) för sms från det delade svarsnumret, som
  svaren routas på (G.1). Numret är inställningens värde när migreringen
  skrevs (UTSKICK_REPLY_NUMBER);
- MonthlyStatement.by_source: bara information (db_default {}).

Beror inte på apps/utskick: sms-appen vet inget om utskicken.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sms", "0005_extra_avsandare"),
    ]

    operations = [
        migrations.AddField(
            model_name="monthlystatement",
            name="by_source",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, output_field=models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="smsmessage",
            name="source",
            field=models.CharField(
                choices=[
                    ("api", "API"),
                    ("utskick", "Utskick"),
                    ("flow", "Flöde"),
                    ("reply", "Svar"),
                    ("system", "Bekräftelse"),
                    ("test", "Test"),
                ],
                db_default="api",
                default="api",
                max_length=10,
                verbose_name="Källa",
            ),
        ),
        migrations.AlterField(
            model_name="smsmessage",
            name="sender",
            field=models.CharField(blank=True, max_length=16, verbose_name="Från"),
        ),
        migrations.AddIndex(
            model_name="smsmessage",
            index=models.Index(fields=["account", "source", "created_at"], name="sms_msg_source"),
        ),
        migrations.AddIndex(
            model_name="smsmessage",
            index=models.Index(
                condition=models.Q(("sender", "+46766860046")),
                fields=["to", "created_at"],
                name="sms_msg_reply_number",
            ),
        ),
    ]
