"""
Byråns sida av ärendesystemet: tavla, ärenden, projekt, kunder, tid.

Allt här är staff-only (access.staff_required). Sidorna renderas i
sajtens egen design (projects/base_board.html), inte panelens - det var
Giovannis uttryckliga krav: "ska se ut som hemsidan".
"""

import csv
import json
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from .access import staff_required
from .board import STAGES, fmt_seconds, move_issue, renumber, with_time
from .emails import send_invite
from .forms import (
    ColumnForm,
    CommentForm,
    CustomerForm,
    InviteForm,
    IssueForm,
    ProjectForm,
)
from .models import (
    Attachment,
    Column,
    Comment,
    Customer,
    Issue,
    Project,
    ProjectStatus,
    TimeEntry,
)


def _json(request):
    try:
        return json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _save_attachments(issue, files, user):
    for f in files:
        Attachment.objects.create(
            issue=issue,
            file=f,
            original_name=f.name[:255],
            content_type=getattr(f, "content_type", "") or "",
            size=f.size,
            uploaded_by=user,
        )


# ------------------------------------------------------------------ tavlan


def _board_issues(project=None, customer=None):
    since = timezone.now() - timedelta(days=30)
    qs = (
        Issue.objects.select_related(
            "project", "column", "customer", "project__customer", "assignee"
        )
        .prefetch_related("labels")
        .with_logged_seconds()
        .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=since))
    )
    if project is not None:
        qs = qs.filter(project=project)
    if customer is not None:
        qs = qs.for_customer(customer)
    return with_time(qs)


def _columns_for(project, issues):
    if project is not None:
        return [
            {
                "key": f"c{c.pk}",
                "title": c.title,
                "wip": c.wip_limit,
                "is_done": c.is_done,
                "issues": [i for i in issues if i.column_id == c.pk],
            }
            for c in project.columns.all()
        ]
    return [
        {
            "key": stage,
            "title": label,
            "wip": None,
            "is_done": stage == "done",
            "issues": [i for i in issues if i.stage == stage],
        }
        for stage, label in STAGES
    ]


@staff_required
def board(request):
    key = request.GET.get("projekt", "").upper()
    projects = Project.objects.exclude(status=ProjectStatus.ARCHIVED).select_related("customer")
    project = projects.filter(key=key).first() if key else None
    issues = _board_issues(project=project)
    columns = _columns_for(project, issues)
    for col in columns:
        col["seconds"] = sum(i.seconds for i in col["issues"])
        col["time"] = fmt_seconds(col["seconds"])
    return render(
        request,
        "projects/board.html",
        {
            "active": "board",
            "projects": projects,
            "project": project,
            "columns": columns,
            "title": project.name if project else "Tavlan",
        },
    )


@staff_required
@require_POST
def issue_move(request, pk):
    issue = get_object_or_404(Issue.objects.select_related("project"), pk=pk)
    data = _json(request)
    target = str(data.get("target", ""))
    order = [int(x) for x in data.get("order", []) if str(x).isdigit()]
    column = stage = None
    if target.startswith("c") and target[1:].isdigit():
        column = get_object_or_404(Column, pk=int(target[1:]))
    elif target in dict(STAGES):
        stage = target
    else:
        return JsonResponse({"ok": False, "error": "okänt mål"}, status=400)
    try:
        move_issue(issue, column=column, stage=stage, after_ids=order)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "stage": issue.stage, "closed": issue.is_closed})


@staff_required
@require_POST
def issues_reorder(request):
    order = [int(x) for x in _json(request).get("order", []) if str(x).isdigit()]
    renumber(order)
    return JsonResponse({"ok": True})


@staff_required
@require_POST
def issue_timer(request, pk):
    issue = get_object_or_404(Issue, pk=pk)
    running = issue.time_entries.filter(user=request.user, ended_at__isnull=True).first()
    stopped = []
    if running:
        running.stop()
    else:
        stopped = list(
            TimeEntry.objects.running().filter(user=request.user).values_list("issue_id", flat=True)
        )
        issue.start_timer(request.user)
    payload = {
        "ok": True,
        "running": running is None,
        "seconds": issue.total_seconds(),
        "stopped": stopped,
        "title": f"{issue.key} {issue.title}",
    }
    if request.headers.get("Content-Type", "").startswith("application/json"):
        return JsonResponse(payload)
    if request.POST.get("next"):
        return redirect(request.POST["next"])
    return redirect("manage:issue_detail", pk=pk)


@staff_required
@require_POST
def timer_stop(request):
    for entry in TimeEntry.objects.running().filter(user=request.user):
        entry.stop()
    return redirect(request.POST.get("next") or "manage:board")


@staff_required
@require_POST
def issue_quick_add(request):
    data = _json(request)
    title = str(data.get("title", "")).strip()[:200]
    if not title:
        return JsonResponse({"ok": False, "error": "rubrik saknas"}, status=400)
    project = Project.objects.filter(key=str(data.get("project", "")).upper()).first()
    issue = Issue(title=title, project=project, reporter=request.user)
    target = str(data.get("target", ""))
    if project and target.startswith("c") and target[1:].isdigit():
        issue.column = Column.objects.filter(pk=int(target[1:]), project=project).first()
    issue.save()
    if not project and target in dict(STAGES):
        move_issue(issue, stage=target)
    elif project and target in dict(STAGES):
        move_issue(issue, stage=target)
    issue = with_time(
        [
            Issue.objects.select_related("project", "column", "customer", "project__customer").get(
                pk=issue.pk
            )
        ]
    )[0]
    html = render_to_string("projects/_card.html", {"issue": issue}, request=request)
    return JsonResponse({"ok": True, "html": html, "id": issue.pk})


# ------------------------------------------------------------------ ärenden


@staff_required
def issue_detail(request, pk):
    issue = get_object_or_404(
        Issue.objects.select_related("project", "column", "customer", "project__customer"), pk=pk
    )
    form = IssueForm(request.POST or None, instance=issue)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{issue.key} sparat.")
        return redirect("manage:issue_detail", pk=pk)
    issue = with_time([issue])[0]
    return render(
        request,
        "projects/issue_detail.html",
        {
            "active": "board",
            "issue": issue,
            "form": form,
            "comment_form": CommentForm(),
            "comments": issue.comments.select_related("author"),
            "attachments": issue.attachments.all(),
            "entries": issue.time_entries.select_related("user")[:50],
            "time_display": fmt_seconds(issue.seconds),
            "title": f"{issue.key} {issue.title}",
        },
    )


@staff_required
@require_POST
def issue_create(request):
    project = Project.objects.filter(key=request.POST.get("project", "").upper()).first()
    customer = Customer.objects.filter(pk=request.POST.get("customer") or 0).first()
    title = request.POST.get("title", "").strip()[:200]
    if not title:
        messages.error(request, "Skriv en rubrik.")
        return redirect(request.POST.get("next") or "manage:board")
    issue = Issue.objects.create(
        title=title, project=project, customer=None if project else customer, reporter=request.user
    )
    return redirect("manage:issue_detail", pk=issue.pk)


@staff_required
@require_POST
def issue_comment(request, pk):
    issue = get_object_or_404(Issue, pk=pk)
    form = CommentForm(request.POST, request.FILES)
    if form.is_valid():
        Comment.objects.create(
            issue=issue,
            author=request.user,
            body=form.cleaned_data["body"],
            is_internal=form.cleaned_data["is_internal"],
        )
        _save_attachments(issue, form.cleaned_data["files"], request.user)
        messages.success(request, "Kommentaren är sparad.")
    else:
        messages.error(
            request,
            "Kommentaren kunde inte sparas: "
            + "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()),
        )
    return redirect("manage:issue_detail", pk=pk)


@staff_required
@require_POST
def issue_attach(request, pk):
    issue = get_object_or_404(Issue, pk=pk)
    form = CommentForm({"body": "-"}, request.FILES)
    if form.is_valid():
        _save_attachments(issue, form.cleaned_data["files"], request.user)
        messages.success(request, f"{len(form.cleaned_data['files'])} bilaga/or uppladdade.")
    else:
        messages.error(request, "; ".join(", ".join(v) for v in form.errors.values()))
    return redirect("manage:issue_detail", pk=pk)


@staff_required
@require_POST
def issue_delete(request, pk):
    issue = get_object_or_404(Issue, pk=pk)
    key = issue.key
    issue.delete()
    messages.success(request, f"{key} är borttaget.")
    return redirect("manage:board")


@staff_required
def attachment_download(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk)
    return FileResponse(
        attachment.file.open("rb"),
        as_attachment=not attachment.is_image,
        filename=attachment.original_name,
    )


# ------------------------------------------------------------------ projekt


@staff_required
def project_list(request):
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.created_by = request.user
        project.save()
        messages.success(request, f"Projektet {project.key} är skapat.")
        return redirect("manage:project_detail", key=project.key)
    projects = Project.objects.select_related("customer").prefetch_related("issues")
    rows = []
    for p in projects:
        open_count = p.issues.filter(closed_at__isnull=True).count()
        rows.append({"project": p, "open": open_count, "time": fmt_seconds(p.total_seconds())})
    return render(
        request,
        "projects/projects.html",
        {"active": "projects", "rows": rows, "form": form, "title": "Projekt"},
    )


@staff_required
def project_detail(request, key):
    project = get_object_or_404(Project.objects.select_related("customer"), key=key.upper())
    form = ProjectForm(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Projektet är sparat.")
        return redirect("manage:project_detail", key=project.key)
    return render(
        request,
        "projects/project_detail.html",
        {
            "active": "projects",
            "project": project,
            "form": form,
            "column_form": ColumnForm(),
            "columns": project.columns.all(),
            "issues": with_time(project.issues.select_related("column").with_logged_seconds()),
            "time": fmt_seconds(project.total_seconds()),
            "title": project.name,
        },
    )


@staff_required
@require_POST
def column_add(request, key):
    project = get_object_or_404(Project, key=key.upper())
    form = ColumnForm(request.POST)
    if form.is_valid():
        column = form.save(commit=False)
        column.project = project
        last = project.columns.order_by("-position").first()
        column.position = (last.position + 1) if last else 0
        column.save()
        messages.success(request, f"Kolumnen {column.title} är tillagd.")
    else:
        messages.error(request, "; ".join(", ".join(v) for v in form.errors.values()))
    return redirect("manage:project_detail", key=project.key)


@staff_required
@require_POST
def column_update(request, pk):
    column = get_object_or_404(Column.objects.select_related("project"), pk=pk)
    action = request.POST.get("action", "save")
    if action == "delete":
        if column.issues.exists():
            messages.error(request, "Kolumnen har ärenden - flytta dem först.")
        else:
            column.delete()
            messages.success(request, "Kolumnen är borttagen.")
    elif action in ("up", "down"):
        siblings = list(column.project.columns.order_by("position", "id"))
        idx = siblings.index(column)
        swap = idx - 1 if action == "up" else idx + 1
        if 0 <= swap < len(siblings):
            siblings[idx], siblings[swap] = siblings[swap], siblings[idx]
            for position, c in enumerate(siblings):
                Column.objects.filter(pk=c.pk).update(position=position)
    else:
        form = ColumnForm(request.POST, instance=column)
        if form.is_valid():
            form.save()
            # Kolumnens is_done kan ha ändrats: Issue.save räknar om closed_at.
            for issue in column.issues.all():
                issue.save()
        else:
            messages.error(request, "; ".join(", ".join(v) for v in form.errors.values()))
    return redirect("manage:project_detail", key=column.project.key)


# ------------------------------------------------------------------ kunder


@staff_required
def customer_list(request):
    form = CustomerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        customer = form.save()
        messages.success(request, f"{customer.name} är skapad.")
        return redirect("manage:customer_detail", pk=customer.pk)
    customers = Customer.objects.prefetch_related("projects", "users")
    rows = [
        {
            "customer": c,
            "open": Issue.objects.for_customer(c).open().count(),
            "time": fmt_seconds(c.total_seconds()),
            "contacts": c.users.count(),
        }
        for c in customers
    ]
    return render(
        request,
        "projects/customers.html",
        {"active": "customers", "rows": rows, "form": form, "title": "Kunder"},
    )


@staff_required
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = CustomerForm(request.POST or None, instance=customer)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Kunden är sparad.")
        return redirect("manage:customer_detail", pk=pk)
    issues = with_time(
        Issue.objects.for_customer(customer)
        .select_related("project", "column")
        .with_logged_seconds()
    )
    return render(
        request,
        "projects/customer_detail.html",
        {
            "active": "customers",
            "customer": customer,
            "form": form,
            "invite_form": InviteForm(),
            "projects": customer.projects.all(),
            "issues": issues,
            "contacts": customer.users.all(),
            "time": fmt_seconds(customer.total_seconds()),
            "title": customer.name,
        },
    )


@staff_required
@require_POST
def customer_invite(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = InviteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Ange en giltig e-postadress.")
        return redirect("manage:customer_detail", pk=pk)
    email = form.cleaned_data["email"].lower()
    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=email,
        defaults={
            "email": email,
            "first_name": form.cleaned_data["first_name"],
            "last_name": form.cleaned_data["last_name"],
            "is_staff": False,
        },
    )
    if user.is_staff:
        messages.error(request, "Den adressen tillhör en byråanvändare.")
        return redirect("manage:customer_detail", pk=pk)
    if created:
        user.set_unusable_password()
        user.save()
    customer.users.add(user)
    if send_invite(user, customer):
        messages.success(request, f"Inbjudan skickad till {email}.")
    else:
        messages.warning(
            request,
            f"{email} är kopplad, men mejlet gick inte iväg. Skicka lösenordslänken manuellt.",
        )
    return redirect("manage:customer_detail", pk=pk)


@staff_required
@require_POST
def customer_remove_contact(request, pk, user_id):
    customer = get_object_or_404(Customer, pk=pk)
    customer.users.remove(user_id)
    messages.success(request, "Kontakten är borttagen från kunden.")
    return redirect("manage:customer_detail", pk=pk)


# ------------------------------------------------------------------ tid


@staff_required
def time_report(request):
    today = date.today()
    start = request.GET.get("from") or today.replace(day=1).isoformat()
    end = request.GET.get("to") or today.isoformat()
    try:
        start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        start_d, end_d = today.replace(day=1), today
    entries = (
        TimeEntry.objects.filter(
            ended_at__isnull=False, started_at__date__gte=start_d, started_at__date__lte=end_d
        )
        .select_related(
            "issue", "issue__project", "issue__project__customer", "issue__customer", "user"
        )
        .order_by("-started_at")
    )
    customer_id = request.GET.get("kund")
    if customer_id and customer_id.isdigit():
        entries = entries.filter(
            Q(issue__project__customer_id=customer_id) | Q(issue__customer_id=customer_id)
        )
    project_key = request.GET.get("projekt", "").upper()
    if project_key:
        entries = entries.filter(issue__project__key=project_key)

    groups = {}
    for e in entries:
        customer = e.issue.effective_customer
        cname = customer.name if customer else "Internt"
        g = groups.setdefault(cname, {"seconds": 0, "billable": 0, "issues": {}})
        g["seconds"] += e.seconds
        g["billable"] += e.seconds if e.is_billable else 0
        row = g["issues"].setdefault(e.issue_id, {"issue": e.issue, "seconds": 0, "entries": 0})
        row["seconds"] += e.seconds
        row["entries"] += 1
    total = sum(g["seconds"] for g in groups.values())

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="tid-{start}-{end}.csv"'
        writer = csv.writer(response, delimiter=";")
        writer.writerow(
            [
                "Datum",
                "Kund",
                "Projekt",
                "Ärende",
                "Rubrik",
                "Minuter",
                "Fakturerbart",
                "Anteckning",
                "Vem",
            ]
        )
        for e in entries:
            customer = e.issue.effective_customer
            writer.writerow(
                [
                    timezone.localtime(e.started_at).strftime("%Y-%m-%d %H:%M"),
                    customer.name if customer else "",
                    e.issue.project.key if e.issue.project_id else "",
                    e.issue.key,
                    e.issue.title,
                    round(e.seconds / 60),
                    "ja" if e.is_billable else "nej",
                    e.note,
                    e.user.get_username(),
                ]
            )
        return response

    return render(
        request,
        "projects/time_report.html",
        {
            "active": "time",
            "groups": groups,
            "total": fmt_seconds(total),
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "customers": Customer.objects.all(),
            "projects": Project.objects.all(),
            "selected_customer": customer_id or "",
            "selected_project": project_key,
            "fmt": fmt_seconds,
            "title": "Tid",
            "group_rows": [
                (
                    name,
                    fmt_seconds(g["seconds"]),
                    fmt_seconds(g["billable"]),
                    [
                        (r["issue"], fmt_seconds(r["seconds"]), r["entries"])
                        for r in g["issues"].values()
                    ],
                )
                for name, g in sorted(groups.items())
            ],
        },
    )
