"""
/manage/services/ - CRUD for service categories, services (with steps), and
audiences. Body/intro fields use the basic Tiptap editor.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.common.security import sanitize_plain_text
from apps.services.models import Audience, Service, ServiceCategory, ServiceStep
from apps.website.models import MediaFile, SiteSettings

from .forms import AudienceForm, ServiceCategoryForm, ServiceForm


def _ctx(**extra):
    settings = SiteSettings.load()
    ctx = {
        "site_settings": settings,
        "active": "services",
        "rot_percentage": settings.rot_percentage,
        "vat_rate": settings.vat_rate,
    }
    ctx.update(extra)
    return ctx


def _form_ctx(**extra):
    """Context for form pages - includes the media list for image pickers."""
    return _ctx(all_media=MediaFile.objects.all(), **extra)


@login_required
def services_overview(request):
    """Landing page for the services section: categories, services, audiences."""
    return render(
        request,
        "manage/services/overview.html",
        _ctx(
            categories=ServiceCategory.objects.select_related("image").all(),
            services=Service.objects.select_related("category", "image").all(),
            audiences=Audience.objects.all(),
        ),
    )


# ----- Categories ----------------------------------------------------------


@login_required
def category_form(request, pk=None):
    instance = get_object_or_404(ServiceCategory, pk=pk) if pk else None
    if request.method == "POST":
        form = ServiceCategoryForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, _("Kategori sparad."))
            return redirect("manage:services_overview")
    else:
        form = ServiceCategoryForm(instance=instance)
    return render(
        request,
        "manage/services/category_form.html",
        _form_ctx(form=form, instance=instance),
    )


@login_required
@require_POST
def category_delete(request, pk):
    category = get_object_or_404(ServiceCategory, pk=pk)
    category.delete()
    messages.success(request, _("Kategori borttagen."))
    return redirect("manage:services_overview")


# ----- Services ------------------------------------------------------------


@login_required
def service_form(request, pk=None):
    instance = get_object_or_404(Service, pk=pk) if pk else None
    if request.method == "POST":
        form = ServiceForm(request.POST, instance=instance)
        if form.is_valid():
            service = form.save()
            _save_steps(request, service)
            messages.success(request, _("Tjänst sparad."))
            return redirect("manage:service_edit", pk=service.pk)
    else:
        form = ServiceForm(instance=instance)
    steps = instance.steps.all() if instance else []
    return render(
        request,
        "manage/services/service_form.html",
        _form_ctx(form=form, instance=instance, steps=steps),
    )


def _save_steps(request, service):
    """
    Rebuild a service's steps from the posted step_title[]/step_desc[] arrays.

    Simple, deterministic: clear and recreate. Empty titles are skipped.
    """
    titles = request.POST.getlist("step_title")
    descs = request.POST.getlist("step_desc")
    service.steps.all().delete()
    order = 0
    for i, title in enumerate(titles):
        clean_title = sanitize_plain_text(title, max_length=200)
        if not clean_title:
            continue
        desc = descs[i] if i < len(descs) else ""
        ServiceStep.objects.create(
            service=service,
            title=clean_title,
            description=sanitize_plain_text(desc, max_length=300),
            order=order,
        )
        order += 1


@login_required
@require_POST
def service_delete(request, pk):
    service = get_object_or_404(Service, pk=pk)
    service.delete()
    messages.success(request, _("Tjänst borttagen."))
    return redirect("manage:services_overview")


# ----- Audiences -----------------------------------------------------------


@login_required
def audience_form(request, pk=None):
    instance = get_object_or_404(Audience, pk=pk) if pk else None
    if request.method == "POST":
        form = AudienceForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, _("Målgrupp sparad."))
            return redirect("manage:services_overview")
    else:
        form = AudienceForm(instance=instance)
    return render(
        request,
        "manage/services/audience_form.html",
        _form_ctx(form=form, instance=instance),
    )


@login_required
@require_POST
def audience_delete(request, pk):
    audience = get_object_or_404(Audience, pk=pk)
    audience.delete()
    messages.success(request, _("Målgrupp borttagen."))
    return redirect("manage:services_overview")
