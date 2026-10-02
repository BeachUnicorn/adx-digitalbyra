"""
Byråns sida av ADX Flamingo, i panelens design: aktivering per kund och en
översikt. Granskningskön och kampanjerna byggs ut med verktyget.

Aktivering mejlar aldrig kunden (webapp/CLAUDE.md: inga automatiska
kundmejl). Vill byrån berätta det görs det manuellt.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.projects.access import VIEW_AS_KEY, staff_required
from apps.projects.models import Customer
from apps.website.models import BlockPage

from .models import FlamingoAccount, account_for


def _back(customer_id):
    return redirect(reverse("manage:customer_detail", args=[customer_id]) + "#flamingo")


def flamingo_card_context(customer):
    """Kundkortets Flamingo-panel."""
    account = FlamingoAccount.objects.filter(customer=customer).select_related("enabled_by").first()
    return {
        "flamingo_account": account,
        "flamingo_enabled": bool(account and account.is_enabled),
        "flamingo_contact_count": customer.users.count(),
    }


@staff_required
@require_POST
def customer_update(request, pk):
    """Slå på eller av ADX Flamingo för kunden. Gäller direkt."""
    customer = get_object_or_404(Customer, pk=pk)
    account = account_for(customer)
    enable = "is_enabled" in request.POST
    if enable and not account.is_enabled:
        account.is_enabled = True
        account.enabled_at = timezone.now()
        account.enabled_by = request.user
        account.save()
        messages.success(
            request,
            f"ADX Flamingo är aktiverat för {customer.name}. Kunden har inte mejlats.",
        )
    elif not enable and account.is_enabled:
        account.is_enabled = False
        account.save(update_fields=["is_enabled", "updated_at"])
        messages.success(request, f"ADX Flamingo är avstängt för {customer.name}.")
    return _back(pk)


@staff_required
@require_POST
def view_as(request, pk):
    """Öppna Flamingo med kundens ögon (samma skrivskyddade kundvy som portalen)."""
    customer = get_object_or_404(Customer, pk=pk, is_active=True)
    request.session[VIEW_AS_KEY] = customer.pk
    return redirect("flamingo:home")


@staff_required
def overview(request):
    """Kunderna med ADX Flamingo och vägarna till sidorna och verktyget."""
    accounts = (
        FlamingoAccount.objects.filter(is_enabled=True)
        .select_related("customer", "enabled_by")
        .order_by("customer__name")
    )
    pages = BlockPage.objects.filter(design=BlockPage.DESIGN_FLAMINGO).order_by("order", "title")
    return render(
        request,
        "manage/flamingo/overview.html",
        {
            "active": "flamingo",
            "title": "ADX Flamingo",
            "accounts": accounts,
            "pages": pages,
            "home_page": pages.filter(slug=BlockPage.FLAMINGO_HOME_SLUG).first(),
            "candidates": Customer.objects.filter(is_active=True)
            .exclude(flamingo__is_enabled=True)
            .order_by("name"),
        },
    )
