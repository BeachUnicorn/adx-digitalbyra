"""
ADX Flamingo mot Google Ads API, körs från cron (README, Integrationer).

    manage.py flamingo_google_sync               alla konton
    manage.py flamingo_google_sync --konto 12    bara Flamingo-kontot 12
    manage.py flamingo_google_sync --dagar 7     rapporten för sju dagar

För varje aktiverat konto (inte demo) hos en aktiv kund, med ett Google
Ads-id:

    1. google_accounts.sync_account_status   kontots läge hos Google
                                             (kopplingen, betalningen)
    2. google_conversions.ensure_conversion_actions
                                             konverteringarna i kundens konto
                                             (CSV-importen behöver dem också)
    3. google_conversions.upload_queued       konverteringarna i kö, bara när
                                             uppladdningen är på (upload_enabled)
    4. google_reports.sync_stats              kostnad, visningar och klick

Steg 2-4 körs bara när kontot ligger under ADX förvaltarkonto
(google_conversions.can_sync). Varje steg körs för sig: ett fel i
konverteringarna stoppar inte rapporten, och tvärtom. Felen sparas
tillsammans i account.google_sync_error ("Konverteringarna: ...",
"Rapporten: ..."), och kontot räknas som misslyckat, men de andra kontona
körs. Ett fel i ADX:s egen koppling (inloggningen) eller en slut kvot
stoppar körningen, också när det kommer från läget i steg 1: då skulle alla
konton få samma fel. Säger Google att ADX inte får ladda upp konverteringar
stoppas steg 3 för alla konton (google_conversions.block_uploads).

Utan Google Ads API (inställningar saknas) skriver kommandot en rad och gör
inget annat. Inga mejl, varken till byrån eller kunden.
"""

import importlib
import logging

from django.core.management.base import BaseCommand

from apps.flamingo import google_ads, google_conversions, google_reports
from apps.flamingo.google_ads import GoogleAdsError
from apps.flamingo.models import FlamingoAccount

logger = logging.getLogger("apps.flamingo.google_sync")

STATUS_MODULE = "apps.flamingo.google_accounts"


def _status_sync():
    """google_accounts.sync_account_status, eller None om modulen saknas."""
    try:
        module = importlib.import_module(STATUS_MODULE)
    except ImportError:
        return None
    return getattr(module, "sync_account_status", None)


class Command(BaseCommand):
    help = "Synkar ADX Flamingo med Google Ads: kontots läge, konverteringar och rapporter."

    def add_arguments(self, parser):
        parser.add_argument("--konto", type=int, help="Bara Flamingo-kontot med det här id:t.")
        parser.add_argument(
            "--dagar",
            type=int,
            default=google_reports.DAYS,
            help="Hur många dagar av rapporten som läses (standard 30).",
        )

    def handle(self, *args, **options):
        if not google_ads.is_configured():
            self.stdout.write("Google Ads API är inte inkopplat, inget synkades.")
            return
        accounts = (
            FlamingoAccount.objects.filter(is_enabled=True, is_demo=False, customer__is_active=True)
            .exclude(google_ads_customer_id="")
            .select_related("customer")
            .order_by("pk")
        )
        if options.get("konto"):
            accounts = accounts.filter(pk=options["konto"])
        status_sync = _status_sync()
        if status_sync is None:
            logger.warning(
                "Google Ads: %s.sync_account_status saknas, steget hoppas över.", STATUS_MODULE
            )

        done = failed = 0
        for account in accounts:
            try:
                summary, problems = self._sync(account, status_sync, options["dagar"])
            except GoogleAdsError as error:
                failed += 1
                self._record(account, error.message)
                logger.warning("Google Ads: konto %s: %s", account.pk, error.message)
                self.stdout.write(f"  {account.customer.name}: {error.message}")
                if error.is_auth_error or error.is_quota_error:
                    self.stdout.write("Stoppad: felet gäller ADX:s koppling till Google.")
                    break
                continue
            except Exception:  # noqa: BLE001 - ett konto får aldrig stoppa de andra
                failed += 1
                logger.exception("Google Ads: oväntat fel för konto %s", account.pk)
                self._record(account, "Oväntat fel vid synken. Se loggen.")
                self.stdout.write(f"  {account.customer.name}: oväntat fel, se loggen.")
                continue
            if problems:
                failed += 1
                for problem in problems:
                    self.stdout.write(f"  {account.customer.name}: {problem}")
                continue
            done += 1
            if summary and options.get("verbosity", 1) >= 2:
                self.stdout.write(f"  {account.customer.name}: {summary}")
        self.stdout.write(f"{done} konto(n) synkade med Google, {failed} med fel.")

    def _sync(self, account, status_sync, days):
        """Ett konto. Returnerar (sammanfattning, fel) där fel är en lista med
        texter; kastar GoogleAdsError bara för ADX-fel (koppling, kvot)."""
        problems = []
        failure_in_status = False
        if status_sync is not None:
            failure = status_sync(account)
            if failure is not None:
                failure_in_status = True
                problems.append(f"Läget: {failure.message}")
            account.refresh_from_db()
        if not google_conversions.can_sync(account):
            return "inte kopplat under ADX förvaltarkonto, bara läget lästes", problems
        parts = []

        def step(label, call):
            try:
                return call()
            except GoogleAdsError as error:
                if error.is_auth_error or error.is_quota_error:
                    raise
                logger.warning("Google Ads: konto %s, %s: %s", account.pk, label, error.message)
                problems.append(f"{label}: {error.message}")
                return None

        step("Konverteringarna", lambda: google_conversions.ensure_conversion_actions(account))
        if google_conversions.upload_enabled():
            uploads = step("Uppladdningen", lambda: google_conversions.upload_queued(account))
            if uploads is not None:
                parts.append(
                    f"{uploads['sent']} konverteringar skickade, {uploads['failed']} "
                    f"misslyckade, {uploads['waiting']} väntar"
                )
        else:
            parts.append("konverteringarna går som CSV")
        stored = step("Rapporten", lambda: google_reports.sync_stats(account, days=days))
        if stored is not None:
            parts.append(f"{stored} dagar i rapporten")
        steps = problems[1:] if failure_in_status else problems
        if steps:
            # Lägets text (ett fel eller en varning, till exempel valutan)
            # står kvar först; sync_account_status har redan sparat den.
            account.refresh_from_db(fields=["google_sync_error"])
            first = [account.google_sync_error] if account.google_sync_error else []
            self._record(account, " ".join([*first, *steps]))
        return "; ".join(parts), problems

    @staticmethod
    def _record(account, message):
        FlamingoAccount.objects.filter(pk=account.pk).update(google_sync_error=message[:300])
