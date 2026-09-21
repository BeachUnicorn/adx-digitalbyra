"""AWS-kontona i /manage/: kundkortets sektion. Allt här är byråns vy."""

import re

from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.projects.access import staff_required
from apps.projects.models import Customer

from . import role_template
from .models import DEFAULT_ROLE_NAME, AwsAccount, AwsInvoice
from .sync import sync_account


def _back(customer_id):
    return redirect(reverse("manage:customer_detail", args=[customer_id]) + "#aws")


@staff_required
@require_POST
def account_add(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    account_id = re.sub(r"\D", "", request.POST.get("account_id", ""))
    if len(account_id) != 12:
        messages.error(request, "Konto-ID är tolv siffror.")
        return _back(pk)
    existing = AwsAccount.objects.filter(account_id=account_id).select_related("customer").first()
    if existing:
        messages.error(request, f"Kontot finns redan på {existing.customer.name}.")
        return _back(pk)
    AwsAccount.objects.create(
        customer=customer,
        account_id=account_id,
        label=request.POST.get("label", "").strip()[:80],
        role_name=request.POST.get("role_name", "").strip()[:64] or DEFAULT_ROLE_NAME,
    )
    messages.success(
        request, "Kontot är tillagt. Skapa läsrollen i kontot och tryck sedan Hämta nu."
    )
    return _back(pk)


@staff_required
@require_POST
def account_update(request, pk):
    account = get_object_or_404(AwsAccount, pk=pk)
    action = request.POST.get("action")
    if action == "delete":
        # Raderna och de sparade PDF:erna försvinner; i AWS ändras ingenting.
        for invoice in account.invoices.exclude(pdf=""):
            invoice.pdf.delete(save=False)
        account.delete()
        messages.success(request, "Kontot är borttaget härifrån. Rollen i AWS tar du bort själv.")
    elif action == "sync":
        ok, text = sync_account(account)
        (messages.success if ok else messages.error)(request, f"{account.display_id}: {text}")
    else:
        account.label = request.POST.get("label", "").strip()[:80]
        account.show_invoices = "show_invoices" in request.POST
        account.is_active = "is_active" in request.POST
        account.save(update_fields=["label", "show_invoices", "is_active"])
        messages.success(request, "Kontot är sparat.")
    return _back(account.customer_id)


@staff_required
def role_file(request, pk):
    account = get_object_or_404(AwsAccount, pk=pk)
    response = HttpResponse(role_template.cloudformation(account), content_type="application/json")
    response["Content-Disposition"] = (
        f'attachment; filename="adx-lasroll-{account.account_id}.json"'
    )
    return response


@staff_required
def invoice_pdf(request, pk):
    invoice = get_object_or_404(AwsInvoice, pk=pk)
    if not invoice.pdf:
        raise Http404
    return FileResponse(invoice.pdf.open("rb"), filename=invoice.filename)
