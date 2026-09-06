"""
Kundportalen /kund/: kundens egna ärenden, inget annat.

Grundregeln sitter i access.customer_issues: kundens ärenden (direkt
eller via projekt) OCH bara de med visible_to_customer. Tid, timers,
interna anteckningar och andra kunder finns inte här - inte dolda med
CSS utan aldrig hämtade.
"""

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy

from .access import customer_for, customer_issues, customer_required
from .board import STAGES
from .emails import send_portal_comment_notice, send_portal_issue_notice
from .forms import MultiFileField, PortalIssueForm
from .models import Attachment, Comment, Issue, IssueType


class PortalLoginView(auth_views.LoginView):
    template_name = "portal/login.html"
    redirect_authenticated_user = True

    def get_success_url(self):
        if self.request.user.is_staff:
            return reverse("manage:board")
        return self.get_redirect_url() or reverse("portal:home")


class PortalLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("portal:login")


class PortalPasswordResetView(auth_views.PasswordResetView):
    template_name = "portal/password_reset_form.html"
    email_template_name = "portal/emails/reset_body.txt"
    subject_template_name = "portal/emails/reset_subject.txt"
    success_url = reverse_lazy("portal:password_reset_done")


class PortalPasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "portal/password_reset_done.html"


class PortalPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "portal/password_reset_confirm.html"
    success_url = reverse_lazy("portal:password_reset_complete")


class PortalPasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "portal/password_reset_complete.html"


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
        {"customer": request.customer, "columns": columns, "title": "Mina ärenden"},
    )


@customer_required
def issue_create(request):
    form = PortalIssueForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        project = request.customer.support_project()
        issue = Issue.objects.create(
            project=project,
            title=form.cleaned_data["title"],
            description=form.cleaned_data["description"],
            issue_type=IssueType.SUPPORT,
            reporter=request.user,
            visible_to_customer=True,
            created_in_portal=True,
        )
        _save_files(issue, form.cleaned_data["files"], request.user)
        send_portal_issue_notice(issue, request.customer)
        messages.success(request, f"Ärendet {issue.key} är skapat. Vi hör av oss.")
        return redirect("portal:issue_detail", pk=issue.pk)
    return render(request, "portal/issue_form.html", {"form": form, "title": "Nytt ärende"})


class PortalCommentForm(PortalIssueForm):
    """Bara text + filer; rubriken används inte."""

    title = None  # type: ignore[assignment]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("title", None)
        self.fields["description"].label = "Kommentar"
        self.fields["description"].required = True
        self.fields["description"].help_text = ""
        self.fields["files"] = MultiFileField(label="Bilagor", required=False)


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
    if customer_for(request.user):
        return redirect("portal:home")
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("manage:board")
    return redirect("portal:login")
