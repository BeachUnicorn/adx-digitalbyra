"""
Byråns sida av ärendesystemet: tavla, ärenden, projekt, kunder, tid.

Allt här är staff-only (access.staff_required) och renderas i panelens
skinn (projects/base_board.html bygger på manage/base.html).

Tavlan är sanningen på servern: varje handling i glidpanelen är ett
POST-anrop som svarar med det som ska ritas om - kortet, panelen och
huvudets siffror - så klienten aldrig gissar. Svaren har alltid formen
{"ok": true, "card": html?, "panel": html?, "stats": {...}?}.

KUNDMEJL: bara issue_email_customer skickar mejl till en kund, och den
nås bara från knappen som säger "Svar + mejl till kunden". Att flytta,
kommentera, stänga eller ändra ett ärende mejlar aldrig någon.
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
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .access import staff_required
from .board import (
    STAGES,
    BoardFilter,
    board_issues,
    board_labels,
    board_projects,
    columns_for,
    fmt_hours,
    fmt_seconds,
    header_stats,
    move_issue,
    renumber,
    with_time,
)
from .emails import send_invite, send_issue_update_to_customer
from .forms import ColumnForm, CommentForm, CustomerForm, InviteForm, ProjectForm
from .models import (
    Attachment,
    ChecklistItem,
    Column,
    Comment,
    Customer,
    Issue,
    IssuePriority,
    IssueType,
    Label,
    Project,
    ProjectStatus,
    TimeEntry,
)


def _json(request):
    try:
        return json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _wants_json(request):
    return request.headers.get("Content-Type", "").startswith(
        "application/json"
    ) or request.headers.get("Accept", "").startswith("application/json")


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


def _staff_users():
    return get_user_model().objects.filter(is_staff=True, is_active=True).order_by("first_name")


def _load_issue(pk):
    return get_object_or_404(
        Issue.objects.select_related(
            "project", "column", "customer", "project__customer", "assignee"
        ).prefetch_related("labels", "checklist"),
        pk=pk,
    )


def _fresh(issue):
    """Ärendet omläst med tid och relationer - efter en ändring."""
    return with_time([_load_issue(issue.pk)])[0]


def _card_html(request, issue):
    return render_to_string("projects/_card.html", {"issue": issue}, request=request)


def _panel_html(request, issue):
    project = issue.project
    if project is not None:
        columns = [(f"c{c.pk}", c.title, c.pk == issue.column_id) for c in project.columns.all()]
    else:
        columns = [(stage, label, stage == issue.stage) for stage, label in STAGES]
    budget = None
    if project is not None and project.budget_hours:
        budget = {
            "logged": fmt_hours(project.total_seconds()),
            "hours": project.budget_hours,
        }
    return render_to_string(
        "projects/_drawer.html",
        {
            "issue": issue,
            "columns": columns,
            "staff": _staff_users(),
            "labels": Label.objects.all(),
            "priorities": IssuePriority.choices,
            "types": IssueType.choices,
            "checklist": list(issue.checklist.all()),
            "entries": issue.time_entries.select_related("user")[:50],
            "comments": issue.comments.select_related("author"),
            "attachments": issue.attachments.all(),
            "activity": issue.activity.select_related("user")[:30],
            "budget": budget,
            "customer": issue.effective_customer,
            "today": timezone.localdate().isoformat(),
        },
        request=request,
    )


def _respond(request, issue=None, *, panel=False, card=True, stats=True, **extra):
    """Standardsvaret: det klienten behöver rita om, inget mer."""
    payload = {"ok": True}
    if issue is not None:
        issue = _fresh(issue)
        if card:
            payload["card"] = _card_html(request, issue)
            payload["stage"] = issue.stage
            payload["closed"] = issue.is_closed
        if panel:
            payload["panel"] = _panel_html(request, issue)
    if stats:
        payload["stats"] = header_stats(request.user)
    payload.update(extra)
    return JsonResponse(payload)


# ------------------------------------------------------------------ tavlan


@staff_required
def board(request):
    flt = BoardFilter(request.GET, user=request.user)
    projects = board_projects()
    project = projects.filter(key=flt.project_key).first() if flt.project_key else None
    if flt.project_key and project is None:
        flt.project_key = ""
    issues = board_issues(flt, project=project)
    columns = columns_for(project, issues)
    open_id = request.GET.get("arende", "")
    return render(
        request,
        "projects/board.html",
        {
            "active": "board",
            "projects": projects,
            "project": project,
            "columns": columns,
            "filter": flt,
            "pills": flt.pills(projects, board_labels()),
            "stats": header_stats(request.user),
            "open_id": int(open_id) if open_id.isdigit() else None,
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
    was = issue.column_id
    try:
        move_issue(issue, column=column, stage=stage, after_ids=order)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    if issue.column_id != was:
        where = issue.column.title if issue.column_id else dict(STAGES).get(stage, "")
        issue.log(request.user, f"flyttade till {where}")
    return _respond(request, issue, panel=bool(data.get("panel")))


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
        if running.seconds >= 60:
            issue.log(request.user, f"loggade {running.seconds // 60} min (timer)")
    else:
        stopped = list(
            TimeEntry.objects.running().filter(user=request.user).values_list("issue_id", flat=True)
        )
        issue.start_timer(request.user)
    if _wants_json(request):
        return _respond(
            request,
            issue,
            panel=bool(_json(request).get("panel")),
            running=running is None,
            seconds=issue.total_seconds(),
            stopped=stopped,
            title=f"{issue.key} {issue.title}",
        )
    if request.POST.get("next"):
        return redirect(request.POST["next"])
    return redirect("manage:issue_detail", pk=pk)


@staff_required
@require_POST
def timer_stop(request):
    for entry in TimeEntry.objects.running().filter(user=request.user):
        entry.stop()
        if entry.seconds >= 60:
            entry.issue.log(request.user, f"loggade {entry.seconds // 60} min (timer)")
    if _wants_json(request):
        return _respond(request)
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
    if target in dict(STAGES):
        move_issue(issue, stage=target)
    issue.log(request.user, "skapade ärendet")
    return _respond(request, issue, id=issue.pk, stats=False)


# ------------------------------------------------------------------ ärenden


@staff_required
def issue_detail(request, pk):
    """
    Djuplänken /manage/arenden/<pk>/ (mejl, MCP-svar) öppnar ärendet i
    glidpanelen på tavlan - det finns bara EN ärendevy.
    """
    issue = get_object_or_404(Issue.objects.select_related("project"), pk=pk)
    params = {"arende": issue.pk}
    if issue.project_id:
        params["projekt"] = issue.project.key
    return redirect(reverse("manage:board") + "?" + "&".join(f"{k}={v}" for k, v in params.items()))


@staff_required
@require_GET
def issue_panel(request, pk):
    issue = _fresh(_load_issue(pk))
    return JsonResponse({"ok": True, "panel": _panel_html(request, issue), "id": issue.pk})


_FIELD_LOG = {
    "priority": lambda i: f"prioritet: {i.get_priority_display()}",
    "assignee": lambda i: (
        f"satte ansvarig: {i.assignee.first_name or i.assignee.get_username()}"
        if i.assignee
        else "tog bort ansvarig"
    ),
    "due_on": lambda i: f"förfaller {i.due_on:%-d %b}" if i.due_on else "tog bort förfallodatum",
    "visible_to_customer": lambda i: (
        "synlig för kund: på" if i.visible_to_customer else "synlig för kund: av"
    ),
}


@staff_required
@require_POST
def issue_field(request, pk):
    """
    Autospar för ETT fält i glidpanelen: {"field": ..., "value": ...}.

    Varje fält saneras för sig - ingen generisk setattr. Kolumnbyte går
    via move_issue så att closed_at och loggen blir rätt.
    """
    issue = _load_issue(pk)
    data = _json(request)
    field = str(data.get("field", ""))
    value = data.get("value")
    update = []

    if field == "title":
        title = str(value or "").strip()[:200]
        if not title:
            return JsonResponse({"ok": False, "error": "Rubriken kan inte vara tom."}, status=400)
        issue.title = title
        update = ["title"]
    elif field == "description":
        issue.description = str(value or "").strip()[:20000]
        update = ["description"]
    elif field == "priority":
        if int(value or 0) not in IssuePriority.values:
            return JsonResponse({"ok": False, "error": "okänd prioritet"}, status=400)
        issue.priority = int(value)
        update = ["priority"]
    elif field == "issue_type":
        if value not in IssueType.values:
            return JsonResponse({"ok": False, "error": "okänd typ"}, status=400)
        issue.issue_type = value
        update = ["issue_type"]
    elif field == "assignee":
        issue.assignee = _staff_users().filter(pk=value).first() if value else None
        update = ["assignee"]
    elif field == "due_on":
        raw = str(value or "").strip()
        try:
            issue.due_on = date.fromisoformat(raw) if raw else None
        except ValueError:
            return JsonResponse({"ok": False, "error": "ogiltigt datum"}, status=400)
        update = ["due_on"]
    elif field == "estimate_minutes":
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        issue.estimate_minutes = min(int(digits), 100_000) if digits else None
        update = ["estimate_minutes"]
    elif field in ("visible_to_customer", "is_billable"):
        setattr(issue, field, bool(value))
        update = [field]
    elif field == "label":
        label = Label.objects.filter(pk=value).first()
        if label is None:
            return JsonResponse({"ok": False, "error": "okänd etikett"}, status=400)
        if issue.labels.filter(pk=label.pk).exists():
            issue.labels.remove(label)
        else:
            issue.labels.add(label)
    elif field == "column":
        target = str(value or "")
        column = stage = None
        if target.startswith("c") and target[1:].isdigit():
            column = Column.objects.filter(pk=int(target[1:]), project=issue.project).first()
            if column is None:
                return JsonResponse({"ok": False, "error": "okänd kolumn"}, status=400)
        elif target in dict(STAGES):
            stage = target
        else:
            return JsonResponse({"ok": False, "error": "okänt mål"}, status=400)
        was = issue.column_id
        move_issue(issue, column=column, stage=stage)
        if issue.column_id != was or not issue.project_id:
            where = issue.column.title if issue.column_id else dict(STAGES).get(stage, "")
            issue.log(request.user, f"flyttade till {where}")
    else:
        return JsonResponse({"ok": False, "error": f"okänt fält: {field}"}, status=400)

    if update:
        issue.save(update_fields=update + ["updated_at"])
        if field in _FIELD_LOG:
            issue.log(request.user, _FIELD_LOG[field](issue))
    return _respond(request, issue, panel=bool(data.get("panel")))


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
    issue.log(request.user, "skapade ärendet")
    return redirect("manage:issue_detail", pk=issue.pk)


@staff_required
@require_POST
def issue_comment(request, pk):
    """
    Kommentar: intern anteckning eller svar i portalen. Mejlar ALDRIG.
    JSON-anrop från glidpanelen, eller vanligt formulär med bilagor.
    """
    issue = _load_issue(pk)
    if _wants_json(request):
        data = _json(request)
        body = str(data.get("body", "")).strip()[:20000]
        if not body:
            return JsonResponse({"ok": False, "error": "Skriv något först."}, status=400)
        internal = bool(data.get("internal", True))
        Comment.objects.create(issue=issue, author=request.user, body=body, is_internal=internal)
        issue.log(request.user, "skrev en intern anteckning" if internal else "svarade i portalen")
        return _respond(request, issue, panel=True)

    form = CommentForm(request.POST, request.FILES)
    if form.is_valid():
        Comment.objects.create(
            issue=issue,
            author=request.user,
            body=form.cleaned_data["body"],
            is_internal=form.cleaned_data["is_internal"],
        )
        _save_attachments(issue, form.cleaned_data["files"], request.user)
        internal = form.cleaned_data["is_internal"]
        issue.log(request.user, "skrev en intern anteckning" if internal else "svarade i portalen")
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
def issue_email_customer(request, pk):
    """
    DEN manuella knappen. Sparar svaret som en kundsynlig kommentar och
    mejlar kunden - inget annat i systemet skickar kundmejl.
    """
    issue = _load_issue(pk)
    data = _json(request) if _wants_json(request) else request.POST
    body = str(data.get("body", "")).strip()[:20000]
    if not body:
        return JsonResponse({"ok": False, "error": "Skriv något först."}, status=400)
    customer = issue.effective_customer
    if customer is None:
        return JsonResponse({"ok": False, "error": "Ärendet har ingen kund."}, status=400)
    comment = Comment.objects.create(issue=issue, author=request.user, body=body, is_internal=False)
    if send_issue_update_to_customer(issue, comment):
        issue.log(request.user, f"mejlade kunden ({customer.name})")
        return _respond(request, issue, panel=True, mailed=True)
    issue.log(request.user, "svarade i portalen (mejlet gick inte iväg)")
    return _respond(
        request,
        issue,
        panel=True,
        mailed=False,
        error="Svaret är sparat, men mejlet gick inte iväg: kunden saknar e-post "
        "eller e-posten är inte konfigurerad.",
    )


@staff_required
@require_POST
def issue_attach(request, pk):
    issue = _load_issue(pk)
    form = CommentForm({"body": "-"}, request.FILES)
    if form.is_valid():
        _save_attachments(issue, form.cleaned_data["files"], request.user)
        n = len(form.cleaned_data["files"])
        issue.log(request.user, f"laddade upp {n} bilaga" + ("" if n == 1 else "or"))
        if request.POST.get("panel"):
            return _respond(request, issue, panel=True, card=False)
        messages.success(request, f"{n} bilaga/or uppladdade.")
    else:
        error = "; ".join(", ".join(v) for v in form.errors.values())
        if request.POST.get("panel"):
            return JsonResponse({"ok": False, "error": error}, status=400)
        messages.error(request, error)
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


# ------------------------------------------------------------------ checklista


@staff_required
@require_POST
def checklist_add(request, pk):
    issue = _load_issue(pk)
    text = str(_json(request).get("text", "")).strip()[:200]
    if not text:
        return JsonResponse({"ok": False, "error": "Skriv en punkt."}, status=400)
    last = issue.checklist.order_by("-position").first()
    ChecklistItem.objects.create(
        issue=issue, text=text, position=(last.position + 1) if last else 0
    )
    return _respond(request, issue, panel=True, stats=False)


@staff_required
@require_POST
def checklist_update(request, pk):
    item = get_object_or_404(ChecklistItem.objects.select_related("issue"), pk=pk)
    data = _json(request)
    issue = item.issue
    if data.get("delete"):
        item.delete()
    else:
        if "done" in data:
            item.is_done = bool(data["done"])
        if "text" in data:
            item.text = str(data["text"]).strip()[:200] or item.text
        item.save(update_fields=["is_done", "text"])
    return _respond(request, issue, panel=bool(data.get("panel")), stats=False)


# ------------------------------------------------------------------ tid


@staff_required
@require_POST
def time_add(request, pk):
    """Tid i efterhand: minuter, valfritt datum och anteckning."""
    issue = _load_issue(pk)
    data = _json(request)
    raw_date = str(data.get("date", "")).strip()
    try:
        on_date = date.fromisoformat(raw_date) if raw_date else timezone.localdate()
    except ValueError:
        return JsonResponse({"ok": False, "error": "ogiltigt datum"}, status=400)
    if on_date > timezone.localdate():
        return JsonResponse({"ok": False, "error": "Datumet ligger i framtiden."}, status=400)
    try:
        entry = TimeEntry.log(
            issue,
            request.user,
            int(str(data.get("minutes", "0")).strip() or 0),
            on_date=on_date,
            note=str(data.get("note", "")).strip(),
        )
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    issue.log(request.user, f"loggade {entry.seconds // 60} min")
    return _respond(request, issue, panel=True)


@staff_required
@require_POST
def time_update(request, pk):
    """Ändra eller ta bort en avslutad tidspost (den egna eller någon annans)."""
    entry = get_object_or_404(TimeEntry.objects.select_related("issue"), pk=pk)
    data = _json(request)
    issue = entry.issue
    if entry.is_running:
        return JsonResponse({"ok": False, "error": "Stoppa timern först."}, status=400)
    if data.get("delete"):
        entry.delete()
        issue.log(request.user, "tog bort en tidspost")
        return _respond(request, issue, panel=True)
    fields = []
    if "minutes" in data:
        try:
            entry.set_minutes(int(str(data["minutes"]).strip() or 0))
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        fields += ["seconds", "started_at"]
    if "note" in data:
        entry.note = str(data["note"]).strip()[:300]
        fields.append("note")
    if "billable" in data:
        entry.is_billable = bool(data["billable"])
        fields.append("is_billable")
    if "date" in data:
        try:
            new_date = date.fromisoformat(str(data["date"]).strip())
        except ValueError:
            return JsonResponse({"ok": False, "error": "ogiltigt datum"}, status=400)
        if new_date > timezone.localdate():
            return JsonResponse({"ok": False, "error": "Datumet ligger i framtiden."}, status=400)
        local = timezone.localtime(entry.ended_at)
        shift = new_date - local.date()
        entry.ended_at += shift
        entry.started_at += shift
        fields += ["ended_at", "started_at"]
    if fields:
        entry.save(update_fields=list(dict.fromkeys(fields)))
        issue.log(request.user, f"ändrade en tidspost till {entry.seconds // 60} min")
    return _respond(request, issue, panel=True)


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
        rows.append({"project": p, "open": open_count, "time": fmt_hours(p.total_seconds())})
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
    quotes = project.quotes.all() if hasattr(project, "quotes") else []
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
            "quotes": quotes,
            "time": fmt_hours(project.total_seconds()),
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
            "time": fmt_hours(c.total_seconds()),
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
            "time": fmt_hours(customer.total_seconds()),
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


def _period(request):
    """Perioden ur ?from/?to eller ett förval: vecka, förra veckan, månad, förra."""
    today = timezone.localdate()
    preset = request.GET.get("period", "")
    monday = today - timedelta(days=today.weekday())
    if preset == "vecka":
        return monday, today, preset
    if preset == "forra-veckan":
        return monday - timedelta(days=7), monday - timedelta(days=1), preset
    if preset == "forra-manaden":
        first = today.replace(day=1)
        last_prev = first - timedelta(days=1)
        return last_prev.replace(day=1), last_prev, preset
    start = request.GET.get("from") or today.replace(day=1).isoformat()
    end = request.GET.get("to") or today.isoformat()
    try:
        return date.fromisoformat(start), date.fromisoformat(end), preset or "manad"
    except ValueError:
        return today.replace(day=1), today, "manad"


@staff_required
def time_report(request):
    start_d, end_d, preset = _period(request)
    start, end = start_d.isoformat(), end_d.isoformat()
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

    groups, days = {}, {}
    for e in entries:
        customer = e.issue.effective_customer
        cname = customer.name if customer else "Internt"
        g = groups.setdefault(cname, {"seconds": 0, "billable": 0, "issues": {}})
        g["seconds"] += e.seconds
        g["billable"] += e.seconds if e.is_billable else 0
        row = g["issues"].setdefault(e.issue_id, {"issue": e.issue, "seconds": 0, "entries": 0})
        row["seconds"] += e.seconds
        row["entries"] += 1
        day = timezone.localtime(e.started_at).date()
        days[day] = days.get(day, 0) + e.seconds
    total = sum(g["seconds"] for g in groups.values())
    billable = sum(g["billable"] for g in groups.values())

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
            "total": fmt_hours(total),
            "billable": fmt_hours(billable),
            "start": start,
            "end": end,
            "preset": preset,
            "customers": Customer.objects.all(),
            "projects": Project.objects.exclude(status=ProjectStatus.ARCHIVED),
            "selected_customer": customer_id or "",
            "selected_project": project_key,
            "title": "Tid",
            "entries": entries[:200],
            "day_rows": [(d, fmt_hours(s)) for d, s in sorted(days.items(), reverse=True)],
            "group_rows": [
                (
                    name,
                    fmt_hours(g["seconds"]),
                    fmt_hours(g["billable"]),
                    [
                        (r["issue"], fmt_hours(r["seconds"]), r["entries"])
                        for r in g["issues"].values()
                    ],
                )
                for name, g in sorted(groups.items())
            ],
            "fmt": fmt_seconds,
        },
    )
