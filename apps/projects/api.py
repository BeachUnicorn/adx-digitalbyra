"""
JSON-API för ADX Fokus, Mac-appen som visar todos vid klockan.

Inloggning med personlig nyckel (AssistantToken, samma som MCP-kopplingen)
i Authorization: Bearer adx_... Nyckeln skapas och återkallas under
/manage/ai/koppling/. Bara byrån: en kundkontakt med nyckel får 403.

Appen pollar GET state/ och varje skrivning svarar med samma state, så
appen alltid har en enda sanning efter ett klick. Timern är serverns:
startar man i appen syns det på tavlan, och tvärtom.

Urvalet är tavlans (board.BoardFilter): kund först (customer = id, "utan"
eller tomt för alla), sedan projekt (project = nyckel). Samma regler för
var en ny todo hamnar.

Inget här mejlar kunden. Att flytta ett ärende till Klart är tyst, precis
som på tavlan.
"""

import json
from functools import wraps

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.common.security import sanitize_plain_text

from .access import is_agency_user
from .board import NO_CUSTOMER, BoardFilter, board_customers, move_issue, today_seconds
from .models import ChecklistItem, Issue, IssueStatus, TimeEntry

#: Fler rader än så ryms inte i en sidopanel ändå.
MAX_ISSUES = 200

#: Klientens namn i aktivitetsloggen.
CLIENT = "ADX Fokus"


def _error(message, status):
    return JsonResponse({"ok": False, "error": message}, status=status)


def api_view(view):
    """Bearer-nyckel -> request.user. Bara byrån, aldrig CSRF (ingen cookie)."""

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        from apps.assistant.models import AssistantToken

        header = request.headers.get("Authorization", "")
        raw = header[7:].strip() if header[:7].lower() == "bearer " else ""
        token = AssistantToken.authenticate(raw)
        if token is None:
            return _error("Ogiltig eller saknad nyckel.", 401)
        if not is_agency_user(token.user):
            return _error("Bara för byrån.", 403)
        request.user = token.user
        return view(request, *args, **kwargs)

    return wrapper


def _body(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _when(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _issue_json(issue):
    customer = issue.effective_customer
    return {
        "id": issue.pk,
        "key": issue.key,
        "title": issue.title,
        "customer": customer.name if customer else None,
        "project": issue.project.key if issue.project_id else None,
        "project_name": issue.project.name if issue.project_id else None,
        "stage": issue.status,
        "priority": issue.priority,
        "priority_label": issue.get_priority_display(),
        "due_on": issue.due_on.isoformat() if issue.due_on else None,
        "is_late": issue.is_late,
        "checklist": [
            {"id": item.pk, "text": item.text, "done": item.is_done}
            for item in issue.checklist.all()
        ],
        "url": f"/manage/arenden/{issue.pk}/",
    }


def _running_json(user):
    entry = TimeEntry.objects.running().filter(user=user).select_related("issue__project").first()
    if entry is None:
        return None
    return {
        "issue_id": entry.issue_id,
        "key": entry.issue.key,
        "title": entry.issue.title,
        "started_at": _when(entry.started_at),
        "seconds": entry.elapsed_seconds(),
    }


def _filter(data):
    return BoardFilter(
        {"kund": data.get("customer") or "", "projekt": data.get("project") or ""}
    ).resolve()


def _add_target(flt):
    """Var en ny todo hamnar i vyn, som text - eller None i Alla."""
    try:
        customer, project = flt.place_for_new()
    except ValueError:
        return None
    if project is not None and project.customer_id:
        return f"{project.customer.name}, {project.name}"
    if project is not None:
        return project.name
    return customer.name if customer else "Utan kund"


def state(user, data):
    """Allt appen visar, i ett svar."""
    flt = _filter(data)
    open_issues = Issue.objects.open()

    loose = open_issues.filter(customer__isnull=True).filter(
        Q(project__isnull=True) | Q(project__customer__isnull=True)
    )
    customers = [{"key": NO_CUSTOMER, "name": "Utan kund", "open": loose.count()}] + [
        {"key": str(c.pk), "name": c.name, "open": open_issues.for_customer(c).count()}
        for c in board_customers(flt.customer)
    ]
    projects = [
        {"key": p.key, "name": p.name, "open": open_issues.filter(project=p).count()}
        for p in flt.projects()
    ]

    issues = flt.scope(
        open_issues.select_related("project", "customer", "project__customer").prefetch_related(
            "checklist"
        )
    )
    issues = list(issues.order_by("created_at", "id")[: MAX_ISSUES + 1])
    truncated = len(issues) > MAX_ISSUES
    issues = issues[:MAX_ISSUES]
    # Pågår före Nytt; inom dem tavlans ordning (egen med kund vald, annars äldst först).
    rank = {IssueStatus.ACTIVE: 0, IssueStatus.NEW: 1}
    if flt.manual_order:
        issues.sort(key=lambda i: (rank.get(i.status, 2), i.position, i.pk))
    else:
        issues.sort(key=lambda i: (rank.get(i.status, 2), i.created_at, i.pk))

    return {
        "ok": True,
        "user": user.first_name or user.get_username(),
        "now": _when(timezone.now()),
        "today_seconds": today_seconds(user),
        "running": _running_json(user),
        "customer": flt.customer_key or None,
        "project": flt.project_key or None,
        "scope_name": flt.scope_name,
        "add_target": _add_target(flt),
        "customers": customers,
        "projects": projects,
        "open_total": open_issues.count(),
        "issues": [_issue_json(i) for i in issues],
        "truncated": truncated,
    }


def _respond(request, data=None):
    data = data if data is not None else _body(request)
    merged = {**request.GET.dict(), **data}
    return JsonResponse(state(request.user, merged))


def _stop_running(user):
    """Stoppa användarens timer och logga den som tavlan gör."""
    for entry in TimeEntry.objects.running().filter(user=user).select_related("issue"):
        entry.stop()
        if entry.seconds >= 60:
            entry.issue.log(user, f"loggade {entry.seconds // 60} min (timer, {CLIENT})")


@api_view
@require_GET
def get_state(request):
    return JsonResponse(state(request.user, request.GET.dict()))


@api_view
@require_POST
def issue_start(request, pk):
    """Starta timern. Ett ärende i Nytt flyttas till Pågår: man har ju börjat."""
    issue = get_object_or_404(Issue.objects.open(), pk=pk)
    _stop_running(request.user)
    issue.start_timer(request.user)
    if issue.status == IssueStatus.NEW:
        move_issue(issue, stage=IssueStatus.ACTIVE)
        issue.log(request.user, "flyttade till Pågår")
    return _respond(request)


@api_view
@require_POST
def timer_stop(request):
    _stop_running(request.user)
    return _respond(request)


@api_view
@require_POST
def issue_done(request, pk):
    issue = get_object_or_404(Issue.objects.open(), pk=pk)
    if issue.time_entries.filter(user=request.user, ended_at__isnull=True).exists():
        _stop_running(request.user)
    move_issue(issue, stage=IssueStatus.DONE)
    issue.log(request.user, "flyttade till Klart")
    return _respond(request)


@api_view
@require_POST
def checklist_toggle(request, pk):
    item = get_object_or_404(ChecklistItem, pk=pk)
    data = _body(request)
    item.is_done = bool(data["done"]) if "done" in data else not item.is_done
    item.save(update_fields=["is_done"])
    return _respond(request, data)


@api_view
@require_POST
def issue_create(request):
    data = _body(request)
    title = sanitize_plain_text(str(data.get("title", "")), max_length=200)
    if not title:
        return _error("Rubriken är tom.", 400)
    try:
        customer, project = _filter(data).place_for_new()
    except ValueError as exc:
        return _error(str(exc), 400)
    issue = Issue.objects.create(
        project=project, customer=customer, title=title, reporter=request.user
    )
    issue.log(request.user, f"skapade ärendet i {CLIENT}")
    return _respond(request, data)
