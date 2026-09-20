"""
Kundportalen /kund/: kundens egna ärenden, inget annat.

Grundregeln sitter i access.customer_issues: kundens ärenden (direkt
eller via projekt) OCH bara de med visible_to_customer. Tid, timers,
interna anteckningar och andra kunder finns inte här - inte dolda med
CSS utan aldrig hämtade.

Inloggningen är lösenordsfri (auth.py): e-post -> engångskod på mejl.
"""

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .access import (
    VIEW_AS_KEY,
    customer_for,
    customer_issues,
    customer_required,
    is_agency_user,
    viewing_customer,
)
from .auth import contact_for_email, issue_code, verify_code
from .board import STAGES
from .emails import send_login_code, send_portal_comment_notice, send_portal_issue_notice
from .forms import PortalCommentForm, PortalIssueForm
from .models import (
    Attachment,
    Comment,
    Issue,
    IssuePriority,
    IssueType,
    RequestKind,
    Urgency,
)

_PRIORITY_FOR_URGENCY = {
    Urgency.CRITICAL: IssuePriority.URGENT,
    Urgency.ASAP: IssuePriority.HIGH,
}

#: Sessionsnycklar mellan e-poststeget och kodsteget.
_PENDING_EMAIL = "portal_login_email"
_PENDING_NEXT = "portal_login_next"


def _safe_next(request, raw):
    if raw and url_has_allowed_host_and_scheme(raw, allowed_hosts={request.get_host()}):
        return raw
    return ""


def login_view(request):
    """Steg 1: e-post. Samma svar oavsett om adressen finns - ingen uppräkning."""
    if customer_for(request.user):
        return redirect("portal:home")
    if request.user.is_authenticated and is_agency_user(request.user):
        return redirect("manage:board")
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()[:254]
        if not email or "@" not in email:
            messages.error(request, "Skriv din e-postadress.")
            return redirect("portal:login")
        user = contact_for_email(email)
        if user is not None:
            code = issue_code(user)
            if code:
                send_login_code(user, code)
        request.session[_PENDING_EMAIL] = email
        request.session[_PENDING_NEXT] = _safe_next(request, request.POST.get("next"))
        return redirect("portal:code")
    return render(
        request,
        "portal/login.html",
        {"title": "Logga in", "next": _safe_next(request, request.GET.get("next"))},
    )


def code_view(request):
    """Steg 2: koden ur mejlet."""
    email = request.session.get(_PENDING_EMAIL)
    if not email:
        return redirect("portal:login")
    if request.method == "POST":
        user = contact_for_email(email)
        if user is not None and verify_code(user, request.POST.get("code", "")):
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            request.session.pop(_PENDING_EMAIL, None)
            target = request.session.pop(_PENDING_NEXT, "") or reverse_lazy("portal:home")
            return redirect(target)
        messages.error(request, "Fel eller utgången kod. Kontrollera mejlet, eller begär en ny.")
    return render(request, "portal/code.html", {"title": "Ange koden", "email": email})


class PortalLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("portal:login")


def _save_files(issue, files, user):
    for f in files:
        Attachment.objects.create(
            issue=issue,
            file=f,
            original_name=f.name[:255],
            content_type=getattr(f, "content_type", "") or "",
            size=f.size,
            uploaded_by=user,
        )


@customer_required
def home(request):
    issues = list(customer_issues(request.customer).order_by("-updated_at"))
    columns = [
        {"key": stage, "title": label, "issues": [i for i in issues if i.stage == stage]}
        for stage, label in STAGES
    ]
    return render(
        request,
        "portal/home.html",
        {
            "customer": request.customer,
            "columns": columns,
            "title": "Mina ärenden",
            "active": "home",
        },
    )


@customer_required
def issue_create(request):
    form = PortalIssueForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        project = request.customer.support_project()
        data = form.cleaned_data
        issue = Issue.objects.create(
            project=project,
            title=data["title"],
            description=data["description"],
            issue_type=IssueType.SUPPORT,
            reporter=request.user,
            visible_to_customer=True,
            created_in_portal=True,
            request_kind=data["request_kind"] or RequestKind.BUILD,
            urgency=data["urgency"] or Urgency.NONE,
            page_url=data["page_url"],
            due_on=data["due_on"],
            # Kundens brådska blir byråns prioritet - det är samma sak sett
            # från två håll, och tavlan ska visa det utan att någon öppnar.
            priority=_PRIORITY_FOR_URGENCY.get(data["urgency"], IssuePriority.NORMAL),
        )
        _save_files(issue, form.cleaned_data["files"], request.user)
        issue.log(request.user, "skapade ärendet i portalen")
        send_portal_issue_notice(issue, request.customer)
        messages.success(request, f"Ärendet {issue.key} är skapat. Vi hör av oss.")
        return redirect("portal:issue_detail", pk=issue.pk)
    return render(
        request, "portal/issue_form.html", {"form": form, "title": "Nytt ärende", "active": "new"}
    )


@customer_required
def issue_detail(request, pk):
    issue = get_object_or_404(customer_issues(request.customer), pk=pk)
    form = PortalCommentForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        comment = Comment.objects.create(
            issue=issue,
            author=request.user,
            body=form.cleaned_data["description"],
            is_internal=False,
        )
        _save_files(issue, form.cleaned_data["files"], request.user)
        issue.log(request.user, "kommenterade i portalen")
        send_portal_comment_notice(comment, request.customer)
        messages.success(request, "Kommentaren är skickad.")
        return redirect("portal:issue_detail", pk=pk)
    return render(
        request,
        "portal/issue_detail.html",
        {
            "issue": issue,
            "stage_label": dict(STAGES)[issue.stage],
            "comments": issue.comments.filter(is_internal=False).select_related("author"),
            "attachments": issue.attachments.all(),
            "form": form,
            "title": issue.title,
            "active": "home",
        },
    )


@customer_required
def attachment_download(request, pk):
    attachment = get_object_or_404(Attachment.objects.select_related("issue"), pk=pk)
    if not customer_issues(request.customer).filter(pk=attachment.issue_id).exists():
        raise Http404
    return FileResponse(
        attachment.file.open("rb"),
        as_attachment=not attachment.is_image,
        filename=attachment.original_name,
    )


def landing(request):
    """/kund/ - inloggad kund till tavlan, annars inloggning."""
    if customer_for(request.user) or viewing_customer(request):
        return redirect("portal:home")
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("manage:board")
    return redirect("portal:login")


@require_POST
def leave_view_as(request):
    """Byrån lämnar kundvyn: tillbaka till kundens sida i panelen."""
    pk = request.session.pop(VIEW_AS_KEY, None)
    if pk and is_agency_user(request.user):
        return redirect("manage:customer_detail", pk=pk)
    return redirect("manage:board")
