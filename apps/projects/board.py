"""
Tavlans gemensamma logik: hinkar, kort-data, flytt, filter och huvudets siffror.

Ett projekt har sina egna kolumner. Vyn "Alla" (byrån) och portalen (kund
med ärenden i flera projekt) behöver ändå en gemensam tavla, och den har
tre hinkar: new / active / done. Issue.stage härleder hinken ur kolumnen,
och move_to_stage gör tvärtom: väljer kolumn i ärendets projekt.

Filtren lever i URL:en (?projekt=NORD&mina=1&forfaller=1&prio=1&etikett=webb
&q=text) så att en vy går att bokmärka och ladda om. Servern filtrerar;
klienten gör bara fritextsökningen omedelbar.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Q, Sum
from django.utils import timezone

from .models import Issue, IssuePriority, Label, Project, ProjectStatus, TimeEntry

STAGES = [("new", "Nytt"), ("active", "Pågår"), ("done", "Klart")]

#: Stängda ärenden ligger kvar på tavlan så här länge (i Klart-kolumnen).
DONE_VISIBLE_DAYS = 30


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
        # Fristående ärende: ingen kolumn, så statusen bärs av två tidsstämplar.
        issue.closed_at = (issue.closed_at or timezone.now()) if stage == "done" else None
        if stage == "new":
            issue.started_at = None
        elif stage == "active":
            issue.started_at = issue.started_at or timezone.now()
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


def fmt_hours(seconds):
    """Timmar och minuter utan sekunder: 2:05. För summor, inte för timern."""
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    return f"{h}:{rem // 60:02d}"


# ------------------------------------------------------------------ filter


class BoardFilter:
    """
    Tavlans filter, lästa ur query-strängen och skrivna tillbaka till den.

    Ett objekt i stället för lösa variabler: vyn, pillren och testerna
    talar samma språk, och en ny flagga läggs till på ett ställe.
    """

    def __init__(self, params, user=None):
        self.project_key = (params.get("projekt") or "").upper()
        self.mine = params.get("mina") == "1"
        self.due = params.get("forfaller") == "1"
        self.prio = params.get("prio") == "1"
        self.label = (params.get("etikett") or "").strip()[:40]
        self.q = (params.get("q") or "").strip()[:100]
        self.user = user

    def as_params(self, **override):
        data = {
            "projekt": self.project_key,
            "mina": "1" if self.mine else "",
            "forfaller": "1" if self.due else "",
            "prio": "1" if self.prio else "",
            "etikett": self.label,
            "q": self.q,
        }
        data.update(override)
        return {k: v for k, v in data.items() if v}

    def url(self, **override):
        params = self.as_params(**override)
        return "?" + urlencode(params) if params else "?"

    @property
    def is_active(self):
        return bool(self.mine or self.due or self.prio or self.label or self.q)

    @property
    def clear_url(self):
        """Allt av utom projektvalet - det är en plats, inte ett filter."""
        return self.url(mina="", forfaller="", prio="", etikett="", q="")

    def apply(self, qs):
        if self.mine and self.user is not None:
            qs = qs.filter(assignee=self.user)
        if self.prio:
            qs = qs.filter(priority__gte=IssuePriority.HIGH)
        if self.label:
            qs = qs.filter(labels__name=self.label)
        if self.due:
            today = timezone.localdate()
            qs = qs.filter(
                closed_at__isnull=True, due_on__isnull=False, due_on__lte=today + timedelta(days=7)
            )
        if self.q:
            qs = qs.filter(
                Q(title__icontains=self.q)
                | Q(project__key__icontains=self.q)
                | Q(project__name__icontains=self.q)
                | Q(project__customer__name__icontains=self.q)
                | Q(customer__name__icontains=self.q)
            )
        return qs.distinct()

    def pills(self, projects, labels):
        """Pillerraden: (etikett, länk, påslagen) i tre grupper."""
        toggles = [
            ("Mina", self.url(mina="" if self.mine else "1"), self.mine),
            ("Förfaller", self.url(forfaller="" if self.due else "1"), self.due),
            ("Hög prio", self.url(prio="" if self.prio else "1"), self.prio),
        ]
        project_pills = [("Alla", self.url(projekt=""), not self.project_key)] + [
            (p.key, self.url(projekt=p.key), self.project_key == p.key) for p in projects
        ]
        label_pills = []
        for lab in labels:
            on = self.label == lab.name
            label_pills.append((lab.name, self.url(etikett="" if on else lab.name), on))
        return {"toggles": toggles, "projects": project_pills, "labels": label_pills}


def board_issues(flt, project=None):
    """Ärendena på tavlan: öppna, plus nyligen stängda, genom filtret."""
    since = timezone.now() - timedelta(days=DONE_VISIBLE_DAYS)
    qs = (
        Issue.objects.select_related(
            "project", "column", "customer", "project__customer", "assignee"
        )
        .prefetch_related("labels", "checklist")
        .with_logged_seconds()
        .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=since))
    )
    if project is not None:
        qs = qs.filter(project=project)
    return with_time(flt.apply(qs))


def columns_for(project, issues):
    """Kolumnerna som visas: projektets egna, eller de tre hinkarna."""
    if project is not None:
        cols = [
            {
                "key": f"c{c.pk}",
                "title": c.title,
                "wip": c.wip_limit,
                "is_done": c.is_done,
                "issues": [i for i in issues if i.column_id == c.pk],
            }
            for c in project.columns.all()
        ]
    else:
        cols = [
            {
                "key": stage,
                "title": label,
                "wip": None,
                "is_done": stage == "done",
                "issues": [i for i in issues if i.stage == stage],
            }
            for stage, label in STAGES
        ]
    for col in cols:
        col["seconds"] = sum(i.seconds for i in col["issues"])
        col["time"] = fmt_hours(col["seconds"])
        col["over"] = bool(col["wip"] and len(col["issues"]) > col["wip"])
    return cols


def board_projects():
    return Project.objects.exclude(status=ProjectStatus.ARCHIVED).select_related("customer")


def board_labels():
    return Label.objects.all()


# ------------------------------------------------------------------ huvudet


def today_seconds(user):
    """Användarens tid i dag: avslutade poster + pågående timer."""
    today = timezone.localdate()
    done = (
        TimeEntry.objects.filter(
            user=user, ended_at__isnull=False, started_at__date=today
        ).aggregate(s=Sum("seconds"))["s"]
        or 0
    )
    live = sum(e.elapsed_seconds() for e in TimeEntry.objects.running().filter(user=user))
    return done + live


def due_counts():
    """(försenade, förfaller inom en vecka) bland öppna ärenden - hela tavlan."""
    today = timezone.localdate()
    open_dated = Issue.objects.filter(closed_at__isnull=True, due_on__isnull=False)
    late = open_dated.filter(due_on__lt=today).count()
    soon = open_dated.filter(due_on__gte=today, due_on__lte=today + timedelta(days=7)).count()
    return late, soon


def header_stats(user):
    """Siffrorna i tavlans huvud, som JSON-vänlig dict (klienten ritar om dem)."""
    running = (
        TimeEntry.objects.running()
        .filter(user=user)
        .select_related("issue", "issue__project")
        .first()
    )
    late, soon = due_counts()
    return {
        "today": fmt_hours(today_seconds(user)),
        "late": late,
        "due": soon,
        "timer": (
            {
                "issue_id": running.issue_id,
                "key": running.issue.key,
                "title": running.issue.title,
                "seconds": running.issue.total_seconds(),
            }
            if running
            else None
        ),
    }
