"""
Stäng månadsunderlagen för SMS-API:t (apps/sms/README.md).

    manage.py sms_close_month                    förra månaden (cron den 1:a)
    manage.py sms_close_month --period 2026-09   en viss månad
    manage.py sms_close_month --dry-run          visa summorna utan att spara

Ett stängt underlag ändras aldrig: körs kommandot igen hoppas det över. Ett
konto med sms från månaden som fortfarande står som reserverade stängs inte
(det rapporteras), och ett konto som inte går att stänga hindrar inte de
andra. Inga mejl, varken till kunden eller byrån.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.sms import pricing
from apps.sms.models import MonthlyStatement, SmsAccount


class Command(BaseCommand):
    help = "Stäng SMS-API:ts månadsunderlag (standard: förra månaden)."

    def add_arguments(self, parser):
        parser.add_argument("--period", help="Månaden som ÅÅÅÅ-MM. Standard: förra månaden.")
        parser.add_argument("--dry-run", action="store_true", help="Spara ingenting.")

    def handle(self, *args, **options):
        if options["period"]:
            try:
                period = pricing.parse_period(options["period"])
            except ValueError:
                raise CommandError("Skriv månaden som ÅÅÅÅ-MM, till exempel 2026-09.") from None
        else:
            period = pricing.previous_month(pricing.current_period())
        if period >= pricing.current_period():
            raise CommandError(f"{period:%Y-%m} har inte tagit slut än.")

        if options["dry_run"]:
            self._dry_run(period)
            return
        result = pricing.close_month(period)
        for statement in result.created:
            self.stdout.write(
                f"{statement.account.customer.name}: {statement.sms_count} sms, "
                f"{pricing.kr_text(statement.total)} kr"
            )
        for account, count in result.waiting:
            self.stdout.write(
                self.style.WARNING(
                    f"{account.customer.name}: inte stängt, {count} sms står som reserverade."
                )
            )
        for account in result.failed:
            self.stderr.write(f"{account.customer.name}: stängningen misslyckades (loggat).")
        style = self.style.WARNING if (result.waiting or result.failed) else self.style.SUCCESS
        self.stdout.write(style(result.summary() + f" {result.empty} utan sms och avgift."))
        if result.failed:
            # Efter att alla andra konton stängts: cron ska se att något föll.
            raise CommandError(f"{len(result.failed)} konton gick inte att stänga.")

    def _dry_run(self, period):
        for account in SmsAccount.objects.select_related("customer").order_by("customer__name"):
            name = account.customer.name
            closed = MonthlyStatement.objects.filter(
                account=account, period=period, closed_at__isnull=False
            ).first()
            statement = closed or pricing.build_statement(account, period)
            if closed:
                state = "redan stängt"
            elif pricing.pending_reservations(period, account).exists():
                state = "väntar på reserverade sms"
            else:
                state = "skulle stängas"
            if statement.sms_count or statement.fee:
                self.stdout.write(
                    f"{name}: {statement.sms_count} sms, "
                    f"{pricing.kr_text(statement.total)} kr ({state})"
                )
        waiting = pricing.pending_reservations(period).count()
        if waiting:
            self.stdout.write(
                self.style.WARNING(
                    f"{waiting} sms från månaden står som reserverade: stäm av dem mot 46elks."
                )
            )
