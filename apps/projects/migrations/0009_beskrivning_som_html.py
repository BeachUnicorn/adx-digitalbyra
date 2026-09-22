# Beskrivningen är HTML från och med Tiptap (apps/projects/richtext.py). De
# befintliga ren-text-beskrivningarna blir stycken, så att radbrytningarna
# inte försvinner när editorn läser in dem.

from django.db import migrations


def to_html(apps, schema_editor):
    from apps.projects.richtext import sanitize_issue_html

    Issue = apps.get_model("projects", "Issue")
    for issue in Issue.objects.exclude(description="").iterator():
        html = sanitize_issue_html(issue.description)
        if html != issue.description:
            Issue.objects.filter(pk=issue.pk).update(description=html)


class Migration(migrations.Migration):
    dependencies = [("projects", "0008_arende_paborjat")]
    operations = [migrations.RunPython(to_html, migrations.RunPython.noop)]
