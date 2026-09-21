"""
Hämtar fakturor och kontobild för kundernas AWS-konton. Körs en gång per dygn.

  manage.py aws_sync                      alla aktiva konton
  manage.py aws_sync --account 123456789012
  manage.py aws_sync --account 123456789012 --profile kund-prod
      utvecklingsvägen: en lokal AWS-profil som redan är kundens konto,
      i stället för läsrollen. Kontot måste ändå finnas på en kund.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.cloud.models import AwsAccount
from apps.cloud.sync import sync_account, sync_all


class Command(BaseCommand):
    help = "Hämtar AWS-fakturor och kontobild."

    def add_arguments(self, parser):
        parser.add_argument("--account", help="Bara detta konto-ID")
        parser.add_argument(
            "--profile", help="Lokal AWS-profil i stället för läsrollen (kräver --account)"
        )
        parser.add_argument("--months", type=int, help="Antal månader bakåt för fakturor")

    def handle(self, *args, **options):
        if options["profile"] and not options["account"]:
            raise CommandError("--profile kräver --account.")
        if options["account"]:
            account = AwsAccount.objects.filter(account_id=options["account"]).first()
            if account is None:
                raise CommandError(
                    "Kontot finns inte på någon kund. Lägg till det på kundkortet först."
                )
            results = [
                (
                    account,
                    *sync_account(account, profile=options["profile"], months=options["months"]),
                )
            ]
        else:
            results = sync_all()
        failed = 0
        for account, ok, text in results:
            failed += not ok
            self.stdout.write(f"{'OK ' if ok else 'FEL'} {account}: {text}")
        if failed:
            raise CommandError(f"{failed} konto(n) gick inte att hämta.")
