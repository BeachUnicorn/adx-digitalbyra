"""
/manage/menus/ - edit the header menu and the footer columns.

Architecture (single-site, mirrors seed_site):
  * The header is ONE Menu (location="header"). Editing it means managing its
    ordered MenuItems.
  * The footer is SEVERAL Menus (location="footer"), one per column. Each has a
    public `heading` and its own ordered MenuItems.

Flow:
  menus_overview  /manage/menus/                       - header items + footer columns
  footer_column_form / _delete                          - add / edit / remove a column
  item_add / item_edit / item_delete / item_move        - manage items in any menu
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.website.models import Menu, MenuItem, SiteSettings

from .forms import FooterColumnForm, MenuItemForm


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "menus"}
    ctx.update(extra)
    return ctx


def _get_header_menu():
    """The header is a singleton menu; create it on first visit."""
    return Menu.objects.filter(location="header").order_by("order", "id").first() or (
        Menu.objects.create(location="header", name="Huvudmeny", order=0)
    )


@login_required
def menus_overview(request):
    header = _get_header_menu()
    footer_columns = []
    for menu in Menu.objects.filter(location="footer").order_by("order", "id"):
        footer_columns.append({"menu": menu, "items": list(menu.items.order_by("order", "id"))})
    return render(
        request,
        "manage/menus/overview.html",
        _ctx(
            header=header,
            header_items=list(header.items.order_by("order", "id")),
            footer_columns=footer_columns,
        ),
    )


# ----- Footer columns ------------------------------------------------------


@login_required
def footer_column_form(request, pk=None):
    instance = get_object_or_404(Menu, pk=pk, location="footer") if pk else None
    if request.method == "POST":
        form = FooterColumnForm(request.POST, instance=instance)
        if form.is_valid():
            column = form.save(commit=False)
            column.location = "footer"
            if column.order is None:
                column.order = (
                    Menu.objects.filter(location="footer").aggregate(m=Max("order"))["m"] or 0
                ) + 1
            column.save()
            messages.success(request, _("Sidfotskolumnen sparades."))
            return redirect("manage:menus_overview")
    else:
        form = FooterColumnForm(instance=instance)
    return render(
        request,
        "manage/menus/column_form.html",
        _ctx(form=form, instance=instance),
    )


@login_required
@require_POST
def footer_column_delete(request, pk):
    column = get_object_or_404(Menu, pk=pk, location="footer")
    column.delete()
    messages.success(request, _("Sidfotskolumnen togs bort."))
    return redirect("manage:menus_overview")


# ----- Menu items (header + footer) ----------------------------------------


@login_required
def item_form(request, menu_pk=None, pk=None):
    """Add a new item to a menu (menu_pk) or edit an existing one (pk)."""
    if pk:
        item = get_object_or_404(MenuItem.objects.select_related("menu"), pk=pk)
        menu = item.menu
    else:
        item = None
        menu = get_object_or_404(Menu, pk=menu_pk)

    if request.method == "POST":
        form = MenuItemForm(request.POST, instance=item)
        if form.is_valid():
            new_item = form.save(commit=False)
            new_item.menu = menu
            if item is None:
                new_item.order = (menu.items.aggregate(m=Max("order"))["m"] or 0) + 1
            new_item.save()
            messages.success(request, _("Menypunkten sparades."))
            return redirect("manage:menus_overview")
    else:
        form = MenuItemForm(instance=item)

    return render(
        request,
        "manage/menus/item_form.html",
        _ctx(form=form, instance=item, menu=menu),
    )


@login_required
@require_POST
def item_delete(request, pk):
    item = get_object_or_404(MenuItem, pk=pk)
    item.delete()
    messages.success(request, _("Menypunkten togs bort."))
    return redirect("manage:menus_overview")


@login_required
@require_POST
def item_toggle(request, pk):
    item = get_object_or_404(MenuItem, pk=pk)
    item.is_visible = not item.is_visible
    item.save(update_fields=["is_visible"])
    return redirect("manage:menus_overview")


@login_required
@require_POST
def item_move(request, pk):
    """Move an item up/down by swapping order with its neighbour in the same menu."""
    item = get_object_or_404(MenuItem.objects.select_related("menu"), pk=pk)
    direction = request.POST.get("direction")
    siblings = list(item.menu.items.order_by("order", "id"))
    index = next((i for i, it in enumerate(siblings) if it.pk == item.pk), None)

    if index is not None:
        swap_with = None
        if direction == "up" and index > 0:
            swap_with = siblings[index - 1]
        elif direction == "down" and index < len(siblings) - 1:
            swap_with = siblings[index + 1]

        if swap_with is not None:
            item.order, swap_with.order = swap_with.order, item.order
            MenuItem.objects.bulk_update([item, swap_with], ["order"])

    return redirect("manage:menus_overview")
