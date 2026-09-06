"""
Tavlans gemensamma logik: hinkar, kort-data och flytt.

Ett projekt har sina egna kolumner. Vyn "Alla" (byrån) och portalen (kund
med ärenden i flera projekt) behöver ändå en gemensam tavla, och den har
tre hinkar: new / active / done. Issue.stage härleder hinken ur kolumnen,
och move_to_stage gör tvärtom: väljer kolumn i ärendets projekt.
"""

from django.db.models import Sum
from django.utils import timezone

from .models import Issue, TimeEntry

STAGES = [("new", "Nytt"), ("active", "Pågår"), ("done", "Klart")]


def column_for_stage(project, stage):
    columns = list(project.columns.order_by("position"))
    if not columns:
        return None
    if stage == "done":
        return next((c for c in columns if c.is_done), columns[-1])
    if stage == "new":
        return columns[0]
    middle = [c for c in columns[1:] if not c.is_done]
    return middle[0] if middle else columns[0]


def move_issue(issue, *, column=None, stage=None, before=None, after_ids=None):
    """Flytta ett ärende till kolumn eller hink, och placera det i ordningen."""
    if column is None and stage and issue.project_id:
        column = column_for_stage(issue.project, stage)
    if column is not None:
        if issue.project_id and column.project_id != issue.project_id:
            raise ValueError("Kolumnen tillhör ett annat projekt.")
        issue.column = column
    elif stage and not issue.project_id:
        # Fristående ärende: bara stängt/öppet finns att välja på.
        issue.closed_at = timezone.now() if stage == "done" else None
    issue.save()
    if after_ids:
        renumber(after_ids)
    return issue


def renumber(ids):
    """Skriv om positionerna för en lista ärende-id i given ordning."""
    issues = {i.pk: i for i in Issue.objects.filter(pk__in=ids)}
    for position, pk in enumerate(ids):
        if pk in issues and issues[pk].position != position:
            issues[pk].position = position
            issues[pk].save(update_fields=["position", "updated_at"])


def with_time(issues):
    """Lägger total_seconds och running (tidspost) på varje ärende i listan."""
    issues = list(issues)
    running = {e.issue_id: e for e in TimeEntry.objects.running().filter(issue__in=issues)}
    for issue in issues:
        logged = getattr(issue, "logged_seconds", None)
        if logged is None:
            logged = (
                issue.time_entries.filter(ended_at__isnull=False).aggregate(s=Sum("seconds"))["s"]
                or 0
            )
        entry = running.get(issue.pk)
        issue.running = entry
        issue.seconds = (logged or 0) + (entry.elapsed_seconds() if entry else 0)
    return issues


def fmt_seconds(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
