"""
/manage/serviceomraden/ - CRUD for the geographic pages.

The edit page is one long form rather than tabs, matching the block/service
editors: a ModelForm for the area's own fields, plus three repeating sections
(child areas, FAQ rows, the service x audience matrix) that are rebuilt from
the POST here.

Repeating rows are keyed by a client-side `uid` (the row's pk, or `new-N` for
unsaved rows) instead of a positional index. Checkboxes only post when checked,
so an index-based scheme silently misaligns as soon as a row is removed in the
browser; a uid survives that.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.areas.models import Area, AreaFAQ, AreaLevel
from apps.common.security import sanitize_plain_text, sanitize_rich_html_basic
from apps.website.models import MediaFile, SiteSettings

from .forms import AreaForm

#: Which level a new child of a given level gets.
CHILD_LEVEL = {
    AreaLevel.REGION: AreaLevel.MUNICIPALITY,
    AreaLevel.MUNICIPALITY: AreaLevel.DISTRICT,
    AreaLevel.DISTRICT: None,
}

#: Section heading and add-button wording for the child panel. Spelled out
#: rather than derived from the level label - "Stadsdel/Ort" doesn't pluralise.
CHILD_LABELS = {
    AreaLevel.REGION: ("Kommuner", "kommun"),
    AreaLevel.MUNICIPALITY: ("Stadsdelar och orter", "ort"),
}

STATUS_FILTERS = [
    ("all", "Alla"),
    ("published", "Publicerade"),
    ("draft", "Utkast"),
    ("hidden", "Dolda av överordnat"),
    ("empty", "Saknar text"),
]

PER_PAGE = 50

#: `level` is a string, so plain ordering would put "district" before
#: "municipality" before "region" - exactly backwards. Rank it explicitly so the
#: list reads top-down: län, sedan kommuner, sedan orter.
LEVEL_RANK = Case(
    When(level=AreaLevel.REGION, then=Value(0)),
    When(level=AreaLevel.MUNICIPALITY, then=Value(1)),
    default=Value(2),
    output_field=IntegerField(),
)


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "areas"}
    ctx.update(extra)
    return ctx


@login_required
def areas_overview(request):
    """Filterable list of every area."""
    level = request.GET.get("level", "")
    status = request.GET.get("status", "all")
    query = (request.GET.get("q") or "").strip()

    areas = (
        Area.objects.select_related("parent", "parent__parent")
        .annotate(
            child_count=Count("children", distinct=True),
            level_rank=LEVEL_RANK,
        )
        .order_by("level_rank", "parent__name", "order", "name")
    )
    if level in AreaLevel.values:
        areas = areas.filter(level=level)
    if query:
        areas = areas.filter(Q(name__icontains=query) | Q(slug__icontains=query))
    if status == "published":
        areas = areas.filter(is_active=True)
    elif status == "draft":
        areas = areas.filter(is_active=False)
    elif status == "empty":
        areas = areas.filter(body="")

    if status == "hidden":
        # Needs the ancestor walk, so it can't be a queryset filter.
        rows = [area for area in areas if area.hidden_by_parent]
    else:
        rows = areas

    page = Paginator(rows, PER_PAGE).get_page(request.GET.get("page"))

    # Query string for the pager, minus `page` itself.
    carry = []
    for key in ("status", "level", "q"):
        value = request.GET.get(key)
        if value:
            carry.append(f"{key}={value}")

    totals = {
        "all": Area.objects.count(),
        "published": Area.objects.filter(is_active=True).count(),
        "draft": Area.objects.filter(is_active=False).count(),
        "empty": Area.objects.filter(body="").count(),
    }

    return render(
        request,
        "manage/areas/overview.html",
        _ctx(
            page=page,
            areas=page.object_list,
            carry="&" + "&".join(carry) if carry else "",
            totals=totals,
            levels=AreaLevel.choices,
            level=level,
            status=status,
            status_filters=STATUS_FILTERS,
            query=query,
        ),
    )


@login_required
def area_form(request, pk=None):
    instance = get_object_or_404(Area, pk=pk) if pk else None

    if request.method == "POST":
        form = AreaForm(request.POST, instance=instance)
        if form.is_valid():
            area = form.save()
            _save_children(request, area)
            _save_faq(request, area)
            messages.success(request, _("Serviceområdet är sparat."))
            return redirect("manage:area_edit", pk=area.pk)
    else:
        form = AreaForm(instance=instance)

    child_level = CHILD_LEVEL.get(instance.level) if instance else None
    child_plural, child_singular = (
        CHILD_LABELS.get(instance.level, ("", "")) if instance else ("", "")
    )
    return render(
        request,
        "manage/areas/area_form.html",
        _ctx(
            form=form,
            instance=instance,
            all_media=MediaFile.objects.all(),
            children=instance.children.order_by("order", "name") if instance else [],
            child_level_label=child_plural if child_level else "",
            child_singular=child_singular,
            faq_items=instance.faq_items.all() if instance else [],
        ),
    )


@login_required
@require_POST
def area_delete(request, pk):
    area = get_object_or_404(Area, pk=pk)
    name = area.name
    area.delete()
    messages.success(request, _("%(name)s är borttaget.") % {"name": name})
    return redirect("manage:areas_overview")


@login_required
@require_POST
def area_toggle(request, pk):
    """Flip is_active straight from the overview list."""
    area = get_object_or_404(Area, pk=pk)
    area.is_active = not area.is_active
    area.save(update_fields=["is_active", "updated_at"])
    state = _("publicerat") if area.is_active else _("dolt")
    messages.success(request, _("%(name)s är nu %(state)s.") % {"name": area.name, "state": state})
    # `next` carries the list's filter/page back. Only accept an in-app path so
    # the parameter can't be turned into an open redirect.
    nxt = request.POST.get("next", "")
    if nxt.startswith("/manage/") and "//" not in nxt[1:]:
        return redirect(nxt)
    return redirect("manage:areas_overview")


# ---------------------------------------------------------------------------
# Repeating sections
# ---------------------------------------------------------------------------


def _save_children(request, area):
    """
    Rebuild the child areas from the posted rows.

    Children carry their own pages, so they are updated in place (never
    recreated) - recreating would change the slug and break inbound links.
    """
    child_level = CHILD_LEVEL.get(area.level)
    if child_level is None:
        return

    uids = request.POST.getlist("child_uid")
    if not uids and "child_uid" not in request.POST:
        return  # section wasn't rendered; leave the children alone

    names = request.POST.getlist("child_name")
    ids = request.POST.getlist("child_id")
    active_uids = set(request.POST.getlist("child_active"))

    existing = {child.pk: child for child in area.children.all()}
    kept = set()

    for index, uid in enumerate(uids):
        name = sanitize_plain_text(names[index] if index < len(names) else "", max_length=120)
        if not name:
            continue
        raw_id = ids[index] if index < len(ids) else ""
        child = existing.get(int(raw_id)) if raw_id.isdigit() else None
        if child is None:
            child = Area(parent=area, level=child_level)
        child.name = name
        child.is_active = uid in active_uids
        child.order = index
        child.save()
        kept.add(child.pk)

    for pk, child in existing.items():
        if pk not in kept:
            child.delete()


def _save_faq(request, area):
    """Clear and recreate - FAQ rows have no URL of their own to preserve."""
    if "faq_question" not in request.POST:
        return
    questions = request.POST.getlist("faq_question")
    answers = request.POST.getlist("faq_answer")

    area.faq_items.all().delete()
    order = 0
    for index, question in enumerate(questions):
        clean_question = sanitize_plain_text(question, max_length=300)
        raw_answer = answers[index] if index < len(answers) else ""
        clean_answer = sanitize_rich_html_basic(raw_answer)
        if not clean_question or not clean_answer:
            continue
        AreaFAQ.objects.create(
            area=area,
            question=clean_question,
            answer=clean_answer,
            order=order,
        )
        order += 1
