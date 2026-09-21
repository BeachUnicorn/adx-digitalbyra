"""AI-koderna i /manage/: skapa, se användning, återkalla."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.projects.access import staff_required

from . import guides
from .models import AccessCode

#: Klartexten visas en gång, direkt efter skapandet, via sessionen.
_FRESH = "aidocs_fresh_code"


@staff_required
def codes(request):
    fresh = request.session.pop(_FRESH, None)
    rows = AccessCode.objects.prefetch_related("log")[:30]
    return render(
        request,
        "manage/aidocs/codes.html",
        {
            "active": "settings",
            "fresh": fresh,
            "rows": rows,
            "guides": guides.GUIDES,
            "now": timezone.now(),
            "base_url": f"{request.scheme}://{request.get_host()}",
        },
    )


@staff_required
@require_POST
def code_create(request):
    hours = 24 if request.POST.get("hours") == "24" else 1
    _obj, code = AccessCode.issue(
        request.user, hours=hours, note=request.POST.get("note", "").strip()
    )
    request.session[_FRESH] = {"code": code, "hours": hours}
    return redirect("manage:ai_codes")


@staff_required
@require_POST
def code_revoke(request, pk):
    code = get_object_or_404(AccessCode, pk=pk)
    if code.revoked_at is None:
        code.revoked_at = timezone.now()
        code.save(update_fields=["revoked_at"])
        messages.success(request, f"Koden ...{code.hint} är återkallad.")
    return redirect("manage:ai_codes")
