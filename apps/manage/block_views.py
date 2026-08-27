"""
/manage/ block editor - a drill-down, not a wall of forms.

Flow:
  1. page_list   /manage/pages/                 - all BlockPages
  2. page_detail /manage/pages/<id>/            - that page's blocks as a list
  3. block_edit  /manage/blocks/<id>/           - edit ONE block, form tailored
                                                  to its block type
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.faq.models import FAQSection
from apps.website.models import Block, BlockPage, BlockType, MediaFile, SiteSettings

from .block_schema import BLOCK_EDIT_SCHEMA, build_form_context, clean_block_data
from .forms import BlockPageForm


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "pages"}
    ctx.update(extra)
    return ctx


@login_required
def page_list(request):
    pages = BlockPage.objects.all().order_by("order", "title")
    return render(request, "manage/pages/list.html", _ctx(pages=pages))


@login_required
def page_form(request, pk=None):
    """Create a new block page or edit an existing page's metadata."""
    instance = get_object_or_404(BlockPage, pk=pk) if pk else None
    if request.method == "POST":
        form = BlockPageForm(request.POST, instance=instance)
        if form.is_valid():
            page = form.save()
            if instance is None:
                messages.success(request, _("Sidan skapades. Lägg till block nedan."))
                return redirect("manage:page_detail", pk=page.pk)
            messages.success(request, _("Sidan sparades."))
            return redirect("manage:page_detail", pk=page.pk)
    else:
        form = BlockPageForm(instance=instance)
    return render(request, "manage/pages/page_form.html", _ctx(form=form, instance=instance))


@login_required
@require_POST
def page_delete(request, pk):
    page = get_object_or_404(BlockPage, pk=pk)
    page.delete()
    messages.success(request, _("Sidan togs bort."))
    return redirect("manage:page_list")


def _block_summary(block):
    """A short human-readable snippet for the block list."""
    data = block.data or {}
    for key in ("title", "label", "height"):
        if data.get(key):
            return str(data[key])[:80]
    # Lists: show a count
    for key in ("cards", "items", "stats", "paragraphs"):
        if data.get(key):
            return f"{len(data[key])} st"
    return ""


@login_required
def page_detail(request, pk):
    page = get_object_or_404(BlockPage, pk=pk)
    blocks = []
    for block in page.blocks.order_by("order"):
        schema = BLOCK_EDIT_SCHEMA.get(block.block_type)
        blocks.append(
            {
                "obj": block,
                "type_label": schema["label"] if schema else block.get_block_type_display(),
                "summary": _block_summary(block),
                "editable": schema is not None,
            }
        )
    # Block types that can be added + edited through this UI.
    add_types = [
        {"value": bt.value, "label": BLOCK_EDIT_SCHEMA[bt.value]["label"]}
        for bt in BlockType
        if bt.value in BLOCK_EDIT_SCHEMA
    ]
    return render(
        request,
        "manage/pages/detail.html",
        _ctx(page=page, blocks=blocks, add_types=add_types),
    )


@login_required
@require_POST
def block_add(request, pk):
    """Append a new block of the chosen type to a page, then open its editor."""
    page = get_object_or_404(BlockPage, pk=pk)
    block_type = request.POST.get("block_type", "")
    if block_type not in BLOCK_EDIT_SCHEMA:
        messages.error(request, _("Okänd blocktyp."))
        return redirect("manage:page_detail", pk=page.pk)

    next_order = (page.blocks.aggregate(m=Max("order"))["m"] or 0) + 1
    block = Block.objects.create(
        page=page,
        block_type=block_type,
        data={},
        order=next_order,
    )
    messages.success(request, _("Blocket lades till. Fyll i innehållet och spara."))
    return redirect("manage:block_edit", pk=block.pk)


@login_required
def block_edit(request, pk):
    block = get_object_or_404(Block.objects.select_related("page"), pk=pk)

    if block.block_type not in BLOCK_EDIT_SCHEMA:
        messages.error(
            request,
            _("Blocktypen %(t)s kan inte redigeras här ännu.") % {"t": block.block_type},
        )
        return redirect("manage:page_detail", pk=block.page_id)

    if request.method == "POST":
        block.data = clean_block_data(block.block_type, request.POST)
        block.is_visible = request.POST.get("is_visible") == "on"
        block.save(update_fields=["data", "is_visible", "updated_at"])
        messages.success(request, _("Blocket sparades."))
        return redirect("manage:page_detail", pk=block.page_id)

    form = build_form_context(block)
    return render(
        request,
        "manage/pages/block_edit.html",
        _ctx(
            block_obj=block,
            form=form,
            all_media=MediaFile.objects.all(),
            all_faq_sections=FAQSection.objects.all(),
        ),
    )


@login_required
@require_POST
def block_toggle(request, pk):
    """Quick show/hide toggle from the block list."""
    block = get_object_or_404(Block, pk=pk)
    block.is_visible = not block.is_visible
    block.save(update_fields=["is_visible", "updated_at"])
    return redirect("manage:page_detail", pk=block.page_id)


@login_required
@require_POST
def block_delete(request, pk):
    """Remove a block from its page."""
    block = get_object_or_404(Block, pk=pk)
    page_id = block.page_id
    block.delete()
    messages.success(request, _("Blocket togs bort."))
    return redirect("manage:page_detail", pk=page_id)


@login_required
@require_POST
def block_move(request, pk):
    """Move a block up or down by swapping its order with its neighbour."""
    block = get_object_or_404(Block.objects.select_related("page"), pk=pk)
    direction = request.POST.get("direction")
    siblings = list(block.page.blocks.order_by("order", "id"))
    index = next((i for i, b in enumerate(siblings) if b.pk == block.pk), None)

    if index is not None:
        swap_with = None
        if direction == "up" and index > 0:
            swap_with = siblings[index - 1]
        elif direction == "down" and index < len(siblings) - 1:
            swap_with = siblings[index + 1]

        if swap_with is not None:
            block.order, swap_with.order = swap_with.order, block.order
            Block.objects.bulk_update([block, swap_with], ["order"])

    return redirect("manage:page_detail", pk=block.page_id)
