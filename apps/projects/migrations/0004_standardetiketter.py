"""
Fyra standardetiketter, bara om tabellen är tom.

Etiketter skapas annars i Django admin, och en tom tavla utan en enda
etikett ser trasig ut. Färgerna är dämpade toner som fungerar mot
panelens ljusa skinn; de går att ändra i admin.
"""

from django.db import migrations

DEFAULT_LABELS = [
    ("webb", "#2b5d84"),
    ("innehall", "#8a6a10"),
    ("drift", "#2e6b45"),
    ("support", "#8a3a3a"),
]


def forwards(apps, schema_editor):
    Label = apps.get_model("projects", "Label")
    if Label.objects.exists():
        return
    for name, color in DEFAULT_LABELS:
        Label.objects.create(name=name, color=color)


class Migration(migrations.Migration):
    dependencies = [("projects", "0003_checklista_aktivitet")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
