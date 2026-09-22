"""Kundkortets AWS-sektion: en rad per konto med det mallen behöver."""

from django.conf import settings

from . import role_template


def aws_card_context(customer):
    rows = []
    for account in customer.aws_accounts.all():
        invoices = list(account.invoices.all())
        rows.append(
            {
                "account": account,
                "invoices": invoices[:24],
                "invoice_count": len(invoices),
                "has_credits": any(
                    m.get("credits") for m in (account.snapshot.get("cost") or {}).get("months", [])
                ),
                "cli": role_template.cli_commands(account),
            }
        )
    return {"aws_rows": rows, "adx_aws_account_id": settings.ADX_AWS_ACCOUNT_ID}
