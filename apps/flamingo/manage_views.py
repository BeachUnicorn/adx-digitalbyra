"""
Byråns sida av ADX Flamingo, i panelens design: aktivering per kund, kundvyn,
översikten och brytaren för utvalda omdömen från Reco. Granskningskön,
publiceringen, filerna och Google-kopplingen finns i manage_review.py.

Aktivering mejlar aldrig kunden (webapp/CLAUDE.md: inga automatiska
kundmejl). Vill byrån berätta det görs det manuellt.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.projects.access import VIEW_AS_KEY, staff_required
from apps.projects.models import Customer
from apps.website.models import BlockPage

from . import reco
from .access import PREFIX
from .models import FlamingoAccount, FlamingoSettings, account_for


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


def _flamingo_next(request):
    """Vart kundvyn öppnas: next ur formuläret om det är en adress i Flamingo
    på den här sajten (verktyget eller en kampanj), annars startsidan."""
    target = request.POST.get("next", "")
    if target.startswith(PREFIX) and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return reverse("flamingo:home")


@staff_required
@require_POST
def view_as(request, pk):
    """Öppna Flamingo med kundens ögon: samma sidor, formulär och knappar som
    kunden har. Det byrån sparar gäller på riktigt, i byråns namn.

    Knapparna i verktygets kundlista och i granskningen skickar next, så att
    byrån hamnar i verktyget eller på kampanjen i stället för på startsidan."""
    customer = get_object_or_404(Customer, pk=pk, is_active=True)
    request.session[VIEW_AS_KEY] = customer.pk
    return redirect(_flamingo_next(request))


@staff_required
def overview(request):
    """Kunderna med ADX Flamingo och vägarna till sidorna och verktyget.
    Demokunden står sist i listan, med en etikett, och räknas inte i
    siffrorna (de är byråns riktiga arbete)."""
    accounts = (
        FlamingoAccount.objects.filter(is_enabled=True)
        .select_related("customer", "enabled_by")
        .order_by("is_demo", "customer__name")
    )
    pages = BlockPage.objects.filter(design=BlockPage.DESIGN_FLAMINGO).order_by("order", "title")
    switch = FlamingoSettings.get_solo()
    return render(
        request,
        "manage/flamingo/overview.html",
        {
            "active": "flamingo",
            "title": "ADX Flamingo",
            "accounts": accounts,
            "customer_count": sum(1 for account in accounts if not account.is_demo),
            "pages": pages,
            "home_page": pages.filter(slug=BlockPage.FLAMINGO_HOME_SLUG).first(),
            "candidates": Customer.objects.filter(is_active=True)
            .exclude(flamingo__is_enabled=True)
            .order_by("name"),
            "reco_switch": switch,
            "reco_selected_on": reco.selected_enabled(),
            "reco_off_by_setting": reco.off_by_setting(),
            "reco_selected_accounts": FlamingoAccount.objects.filter(is_demo=False)
            .exclude(reco_reviews_selected=[])
            .count(),
        },
    )


@staff_required
@require_POST
def selected_reviews_switch(request):
    """Brytaren för utvalda omdömen från Reco (FlamingoSettings,
    reco.selected_enabled): av gäller direkt för alla kunder. Sidorna visar
    då Recos egen ruta, inget hämtas från Reco, och valet i verktyget göms.
    Kunderna mejlas inte."""
    switch = FlamingoSettings.get_solo()
    turn_on = request.POST.get("enabled") == "1"
    if switch.reco_selected_enabled != turn_on:
        switch.reco_selected_enabled = turn_on
        switch.reco_selected_changed_at = timezone.now()
        switch.reco_selected_changed_by = request.user
        switch.save()
    if turn_on and reco.off_by_setting():
        messages.warning(
            request,
            "Knappen står på, men FLAMINGO_RECO_SELECTED_ENABLED är false i miljön, så "
            "utvalda omdömen från Reco är fortfarande av.",
        )
    elif turn_on:
        messages.success(request, "Utvalda omdömen från Reco är på igen.")
    else:
        messages.success(
            request,
            "Utvalda omdömen från Reco är avstängda för alla. Sidorna visar Recos egen ruta, "
            "och inget hämtas. Kunderna har inte mejlats.",
        )
    return redirect(reverse("manage:flamingo_overview") + "#reco")
