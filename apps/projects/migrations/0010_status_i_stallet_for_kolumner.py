"""
Status i stället för kolumner, steg 1: fältet och datan.

Samma regel som Issue.stage hade: stängt = Klart; annars kolumnens plats
(första = Nytt, en senare som inte stänger = Pågår); ärenden utan projekt
bar Pågår i started_at. Kolumnerna tas bort i nästa migrering.
"""

from django.db import migrations, models


def to_status(apps, schema_editor):
    Issue = apps.get_model("projects", "Issue")
    for issue in Issue.objects.select_related("column").iterator():
        if issue.closed_at or (issue.column_id and issue.column.is_done):
            status = "done"
        elif issue.column_id:
            status = "active" if issue.column.position > 0 else "new"
        else:
            status = "active" if issue.started_at else "new"
        if issue.status != status:
            Issue.objects.filter(pk=issue.pk).update(status=status)


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0009_beskrivning_som_html"),
    ]

    operations = [
        migrations.AddField(
            model_name="issue",
            name="status",
            field=models.CharField(
                choices=[("new", "Nytt"), ("active", "Pågår"), ("done", "Klart")],
                default="new",
                max_length=10,
                verbose_name="Status",
            ),
        ),
        migrations.RunPython(to_status, migrations.RunPython.noop),
    ]
