"""
Collapse already-recorded thank-you page paths to the placeholder form.

Without this, rows written before path normalisation keep one entry per
inquiry reference and continue to crowd out real pages in the reports.
"""

from django.db import migrations

OLD_PREFIX = "/forfragan/tack/"
NEW_PATH = "/forfragan/tack/:referens/"


def normalize(apps, schema_editor):
    for model_name in ("PageView", "Event"):
        model = apps.get_model("analytics", model_name)
        (
            model.objects.filter(path__startswith=OLD_PREFIX)
            .exclude(path=NEW_PATH)
            .update(path=NEW_PATH)
        )


def noop(apps, schema_editor):
    """The original references are gone; nothing to restore."""


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0002_event_placement_pageview_engaged_seconds_and_more"),
    ]

    operations = [
        migrations.RunPython(normalize, noop),
    ]
