"""
Tavlans gemensamma logik: kolumner, kort-data, flytt, filter och huvudets siffror.

Tre fasta kolumner för allt (Issue.status): Nytt, Pågår, Klart. Samma på
byråns tavla, i portalen och i Mac-appen.

Man väljer KUND först och smalnar sedan av till ett projekt:

    Alla        allt, även ärenden utan kund. Äldst skapade överst.
    Utan kund   lösa ärenden och projekt som inte hör till någon kund.
    <kund>      alla kundens ärenden, oavsett projekt (eller inget).
      <projekt> bara det projektet.

Kunden är enheten man tänker i och prioriterar inom; projekten är byråns
egna fack. Med projekt först hamnade en kunds arbete på flera ställen, och
ärenden direkt på kunden syntes inte alls.

Filtren lever i URL:en (?kund=3&projekt=NORD&mina=1&forfaller=1&prio=1
&etikett=webb&q=text) så att en vy går att bokmärka och ladda om. Servern
filtrerar; klienten gör bara fritextsökningen omedelbar.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    Customer,
    Issue,
    IssuePriority,
    IssueStatus,
    Label,
    Project,
    ProjectStatus,
    TimeEntry,
)

STAGES = list(IssueStatus.choices)

#: Stängda ärenden ligger kvar på tavlan så här länge (i Klart-kolumnen).
DONE_VISIBLE_DAYS = 30

#: ?kund=utan - ärenden och projekt utan kund.
NO_CUSTOMER = "utan"


def move_issue(issue, *, stage, after_ids=None):
    """Flytta ett ärende till en kolumn (new/active/done) och placera det i ordningen."""
    if stage not in IssueStatus.values:
        raise ValueError("Okänd kolumn. Använd new, active eller done.")
    issue.status = stage
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

    Ett objekt i stället för lösa variabler: vyn, pillren, API:t och
    testerna talar samma språk, och en ny flagga läggs till på ett ställe.
    Anropa resolve() innan kund och projekt används.
    """

    def __init__(self, params, user=None):
        raw = str(params.get("kund") or "").strip().lower()
        self.customer_key = raw if raw == NO_CUSTOMER or raw.isdigit() else ""
        self.project_key = str(params.get("projekt") or "").strip().upper()
        self.mine = params.get("mina") == "1"
        self.due = params.get("forfaller") == "1"
        self.prio = params.get("prio") == "1"
        self.label = str(params.get("etikett") or "").strip()[:40]
        self.q = str(params.get("q") or "").strip()[:100]
        self.user = user
        self.customer = None
        self.project = None

    def resolve(self):
        """
        Slå upp kund och projekt. Ett ogiltigt val faller tillbaka till den
        bredare vyn i stället för att ge en tom tavla. Ett projekt utan
        kundval (gamla länkar, ?projekt=NORD) väljer sin kund själv.
        """
        if self.customer_key.isdigit():
            self.customer = Customer.objects.filter(pk=int(self.customer_key)).first()
            if self.customer is None:
                self.customer_key = ""
        if self.project_key:
            project = (
                Project.objects.select_related("customer").filter(key=self.project_key).first()
            )
            if project is not None and not self.customer_key:
                self.customer_key = str(project.customer_id) if project.customer_id else NO_CUSTOMER
                self.customer = project.customer
            if project is not None and project.customer_id != (
                self.customer.pk if self.customer else None
            ):
                project = None
            self.project = project
            if project is None:
                self.project_key = ""
        return self

    @property
    def is_all(self):
        return not self.customer_key

    @property
    def manual_order(self):
        """Egen ordning (dra korten) när en kund är vald; "Alla" sorterar på datum."""
        return not self.is_all

    @property
    def scope_name(self):
        if self.project:
            return self.project.name
        if self.customer:
            return self.customer.name
        return "Utan kund" if self.customer_key == NO_CUSTOMER else "Alla kunder"

    def as_params(self, **override):
        data = {
            "kund": self.customer_key,
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
        """Allt av utom kund och projekt - det är en plats, inte ett filter."""
        return self.url(mina="", forfaller="", prio="", etikett="", q="")

    def scope(self, qs):
        """Bara kund- och projektvalet - det som avgör var man är."""
        if self.customer is not None:
            qs = qs.for_customer(self.customer)
        elif self.customer_key == NO_CUSTOMER:
            qs = qs.filter(customer__isnull=True).filter(
                Q(project__isnull=True) | Q(project__customer__isnull=True)
            )
        if self.project is not None:
            qs = qs.filter(project=self.project)
        return qs

    def apply(self, qs):
        qs = self.scope(qs)
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

    def projects(self):
        """Projekten man kan smalna av till under vald kund (inga under Alla)."""
        if self.customer is not None:
            qs = self.customer.projects.all()
        elif self.customer_key == NO_CUSTOMER:
            qs = Project.objects.filter(customer__isnull=True)
        else:
            return Project.objects.none()
        return qs.exclude(status=ProjectStatus.ARCHIVED).order_by("name")

    def place_for_new(self):
        """
        (kund, projekt) för ett nytt ärende i den valda vyn.

        Projekt valt: där. Kund vald med ETT aktivt projekt: i det. Kund med
        flera: direkt på kunden. Utan kund: helt löst. Alla: inget hem -
        ValueError, överblicken är inte en plats att lägga saker på.
        """
        if self.project is not None:
            return None, self.project
        if self.customer is not None:
            active = list(self.customer.projects.filter(status=ProjectStatus.ACTIVE)[:2])
            return (None, active[0]) if len(active) == 1 else (self.customer, None)
        if self.customer_key == NO_CUSTOMER:
            return None, None
        raise ValueError("Välj en kund, eller Utan kund, för att lägga till.")

    def pills(self, customers, labels):
        """Pillerraden: (etikett, länk, påslagen) i grupper."""
        toggles = [
            ("Mina", self.url(mina="" if self.mine else "1"), self.mine),
            ("Förfaller", self.url(forfaller="" if self.due else "1"), self.due),
            ("Hög prio", self.url(prio="" if self.prio else "1"), self.prio),
        ]
        customer_pills = [
            ("Alla", self.url(kund="", projekt=""), self.is_all),
            ("Utan kund", self.url(kund=NO_CUSTOMER, projekt=""), self.customer_key == NO_CUSTOMER),
        ] + [
            (c.name, self.url(kund=str(c.pk), projekt=""), self.customer_key == str(c.pk))
            for c in customers
        ]
        project_pills = []
        projects = list(self.projects())
        if projects:
            project_pills = [("Alla projekt", self.url(projekt=""), not self.project_key)] + [
                (p.name, self.url(projekt=p.key), self.project_key == p.key) for p in projects
            ]
        label_pills = []
        for lab in labels:
            on = self.label == lab.name
            label_pills.append((lab.name, self.url(etikett="" if on else lab.name), on))
        return {
            "toggles": toggles,
            "customers": customer_pills,
            "projects": project_pills,
            "labels": label_pills,
        }


def board_customers(selected=None):
    """Aktiva kunder, plus den valda om den har gjorts inaktiv."""
    qs = Customer.objects.filter(Q(is_active=True) | Q(pk=getattr(selected, "pk", None)))
    return qs.order_by("name")


def board_issues(flt):
    """Ärendena på tavlan: öppna, plus nyligen stängda, genom filtret."""
    since = timezone.now() - timedelta(days=DONE_VISIBLE_DAYS)
    qs = (
        Issue.objects.select_related("project", "customer", "project__customer", "assignee")
        .prefetch_related("labels", "checklist")
        .with_logged_seconds()
        .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=since))
    )
    return with_time(flt.apply(qs))


def order_column(issues, status, manual):
    """
    Klart: senast stängt överst. Nytt och Pågår: egen ordning när en kund
    är vald, annars äldst skapat överst.
    """
    if status == IssueStatus.DONE:
        return sorted(issues, key=lambda i: (i.closed_at or i.created_at, i.pk), reverse=True)
    if manual:
        return sorted(issues, key=lambda i: (i.position, i.pk))
    return sorted(issues, key=lambda i: (i.created_at, i.pk))


def columns_for(issues, manual=False):
    """De tre kolumnerna med sina kort, i rätt ordning."""
    cols = []
    for status, label in STAGES:
        col_issues = order_column([i for i in issues if i.status == status], status, manual)
        seconds = sum(i.seconds for i in col_issues)
        cols.append(
            {
                "key": status,
                "title": label,
                "is_done": status == IssueStatus.DONE,
                "issues": col_issues,
                "seconds": seconds,
                "time": fmt_hours(seconds),
            }
        )
    return cols


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
