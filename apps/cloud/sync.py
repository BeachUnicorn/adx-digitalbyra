"""
Hämtningen: fakturor (rader + PDF) och byråns ögonblicksbild.

Första gången hämtas tretton månader bakåt, sedan bara de tre senaste -
AWS kan ge ut en kreditnota eller en sen faktura för en stängd period.
En PDF hämtas EN gång och sparas privat; saknas den försöker nästa körning
igen. Ett fel på ett konto stoppar aldrig nästa.
"""

import logging

from django.core.files.base import ContentFile
from django.utils import timezone

from . import aws
from .models import AwsAccount, AwsInvoice

logger = logging.getLogger(__name__)

FIRST_SYNC_MONTHS = 13
REGULAR_SYNC_MONTHS = 3


def sync_invoices(account, session, months=None):
    if months is None:
        months = REGULAR_SYNC_MONTHS if account.invoices.exists() else FIRST_SYNC_MONTHS
    rows = aws.fetch_invoices(session, account.account_id, aws.months_back(months))
    new = 0
    for row in rows:
        invoice_id = row.pop("invoice_id")
        _, created = AwsInvoice.objects.update_or_create(
            account=account, invoice_id=invoice_id, defaults=row
        )
        new += created
    pdfs = 0
    for invoice in account.invoices.filter(pdf=""):
        try:
            data = aws.download_pdf(session, invoice.invoice_id)
        except (aws.AwsError, OSError) as exc:
            logger.warning("Faktura-PDF %s: %s", invoice.invoice_id, exc)
            continue
        invoice.pdf.save(f"{invoice.invoice_id}.pdf", ContentFile(data), save=True)
        pdfs += 1
    return {"invoices": len(rows), "new": new, "pdfs": pdfs}


def sync_account(account, *, profile=None, months=None, snapshot=True):
    """Returnerar (ok, text). Skriver alltid last_sync_at och ev. fel på kontot."""
    account.last_sync_at = timezone.now()
    try:
        session = aws.session_for(account, profile=profile)
        result = sync_invoices(account, session, months=months)
        if snapshot:
            account.snapshot = aws.fetch_snapshot(session)
    except aws.AwsError as exc:
        account.last_error = str(exc)[:300]
        account.save(update_fields=["last_sync_at", "last_error"])
        return False, account.last_error
    account.last_error = ""
    account.last_ok_at = account.last_sync_at
    account.save(update_fields=["last_sync_at", "last_ok_at", "last_error", "snapshot"])
    text = f"{result['invoices']} fakturor ({result['new']} nya, {result['pdfs']} PDF hämtade)"
    return True, text


def sync_all():
    results = []
    for account in AwsAccount.objects.filter(is_active=True, customer__is_active=True):
        results.append((account, *sync_account(account)))
    return results
