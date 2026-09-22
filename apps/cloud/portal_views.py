"""
Kundportalens enda AWS-sida: fakturorna, för kundens bokföring.

Kostnad, resurser och säkerhet visas aldrig här - det är byråns bild.
Varje faktura lämnas ut bara till kunden som äger kontot, och bara om
rutan "Kunden ser fakturorna" är ikryssad på kontot.
"""

from itertools import groupby

from django.http import FileResponse, Http404
from django.shortcuts import render

from apps.projects.access import customer_required

from .models import AwsInvoice


def visible_invoices(customer):
    return AwsInvoice.objects.filter(
        account__customer=customer, account__show_invoices=True
    ).select_related("account")


@customer_required
def invoices(request):
    rows = list(visible_invoices(request.customer))
    years = [
        {"year": year, "invoices": list(items)}
        for year, items in groupby(rows, key=lambda i: i.period_year)
    ]
    accounts = {i.account_id for i in rows}
    return render(
        request,
        "portal/invoices.html",
        {
            "title": "Fakturor",
            "active": "invoices",
            "customer": request.customer,
            "years": years,
            "several_accounts": len(accounts) > 1,
            "has_credits": any(i.credits for i in rows),
        },
    )


@customer_required
def invoice_pdf(request, pk):
    invoice = visible_invoices(request.customer).filter(pk=pk).first()
    if invoice is None or not invoice.pdf:
        raise Http404
    return FileResponse(invoice.pdf.open("rb"), filename=invoice.filename)
