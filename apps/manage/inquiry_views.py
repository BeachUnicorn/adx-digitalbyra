"""
/manage/inquiries/ - View and manage incoming quote requests.

Staff can:
- See a list of all inquiries (newest first, unread highlighted)
- View full details including uploaded images
- Change status (Ny → Under hantering → Offert skickad → Avslutad)
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.inquiries.models import Inquiry, InquiryStatus
from apps.website.models import SiteSettings


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "inquiries"}
    ctx.update(extra)
    return ctx


@login_required
def inquiry_list(request):
    """List all inquiries, newest first."""
    status_filter = request.GET.get("status", "")
    inquiries = Inquiry.objects.all()

    if status_filter:
        inquiries = inquiries.filter(status=status_filter)

    return render(
        request,
        "manage/inquiries/list.html",
        _ctx(
            inquiries=inquiries,
            status_filter=status_filter,
            status_choices=InquiryStatus.choices,
            unread_count=Inquiry.objects.filter(is_read=False).count(),
        ),
    )


@login_required
def inquiry_detail(request, pk):
    """View a single inquiry with all details and images."""
    inquiry = get_object_or_404(Inquiry, pk=pk)

    # Mark as read on first view
    if not inquiry.is_read:
        inquiry.is_read = True
        inquiry.save(update_fields=["is_read"])

    return render(
        request,
        "manage/inquiries/detail.html",
        _ctx(
            inquiry=inquiry,
            images=inquiry.images.all(),
            status_choices=InquiryStatus.choices,
        ),
    )


@login_required
@require_POST
def inquiry_status(request, pk):
    """Update the status of an inquiry."""
    inquiry = get_object_or_404(Inquiry, pk=pk)
    new_status = request.POST.get("status", "")

    if new_status in InquiryStatus.values:
        inquiry.status = new_status
        inquiry.save(update_fields=["status"])
        messages.success(request, _("Status uppdaterad."))
    else:
        messages.error(request, _("Ogiltig status."))

    return redirect("manage:inquiry_detail", pk=pk)
