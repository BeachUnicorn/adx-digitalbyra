"""
ADX Flamingo mot Google Ads API, körs från cron (README, Integrationer).

    manage.py flamingo_google_sync               alla konton
    manage.py flamingo_google_sync --konto 12    bara Flamingo-kontot 12
    manage.py flamingo_google_sync --dagar 7     rapporten för sju dagar
    manage.py flamingo_google_sync --prova       konverteringarna prövas av
                                                 Google (validateOnly) men
                                                 skickas inte, och inget
                                                 ändras på raderna

För varje aktiverat konto (inte demo) hos en aktiv kund, med ett Google
Ads-id:

    1. google_accounts.sync_account_status   kontots läge hos Google
                                             (kopplingen, betalningen)
    2. google_conversions.ensure_conversion_actions
                                             konverteringarna i kundens konto
                                             (CSV-importen behöver dem också)
    3. google_conversions.upload_queued       konverteringarna i kö den valda
                                             vägen (FLAMINGO_CONVERSIONS_UPLOAD,
                                             Data Manager API från början), bara
                                             när uppladdningen är på
                                             (upload_enabled)
    4. google_conversions.check_sent          Googles besked om det som skickats
                                             med Data Manager API, oavsett vald
                                             väg (verdicts_enabled)
    5. google_reports.sync_stats              kostnad, visningar och klick

Steg 2-5 körs bara när kontot ligger under ADX förvaltarkonto
(google_conversions.can_sync). Varje steg körs för sig: ett fel i
konverteringarna stoppar inte rapporten, och tvärtom. Felen sparas
tillsammans i account.google_sync_error ("Konverteringarna: ...",
"Rapporten: ..."), och kontot räknas som misslyckat, men de andra kontona
körs. Ett fel i ADX:s egen koppling (inloggningen) eller en slut kvot
stoppar körningen, också när det kommer från läget i steg 1: då skulle alla
konton få samma fel. Säger Google nej till hela vägen för konverteringarna
(ADX inte på Googles lista, Data Manager API avslaget i Cloud-projektet,
behörigheten saknas) stoppas steg 3 och 4 för alla konton, Google-sidan
säger varför och kommandot skriver det en gång; det räknas inte som ett fel
för kontot. Raderna står kvar i kö för CSV-filen.

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
        parser.add_argument(
            "--prova",
            action="store_true",
            help=(
                "Låt Google pröva konverteringarna i kö (Data Manager API, validateOnly) utan "
                "att skicka dem. Inget ändras på raderna."
            ),
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

        #: Googles nej till hela vägen för konverteringarna (UPLOAD_NOT_ALLOWED),
        #: skrivet en gång efter kontona.
        self._stopped_uploads = ""
        done = failed = 0
        for account in accounts:
            try:
                summary, problems = self._sync(
                    account, status_sync, options["dagar"], options.get("prova", False)
                )
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
        if self._stopped_uploads:
            self.stdout.write(f"Konverteringarna stoppade: {self._stopped_uploads}")
        self.stdout.write(f"{done} konto(n) synkade med Google, {failed} med fel.")

    def _sync(self, account, status_sync, days, validate_only=False):
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
                if error.status == "UPLOAD_NOT_ALLOWED":
                    # Gäller alla konton, inte det här: Google-sidan säger varför.
                    self._stopped_uploads = error.message
                    parts.append("konverteringarna stoppade av Google, se Google-sidan")
                    return None
                if error.is_auth_error or error.is_quota_error:
                    raise
                logger.warning("Google Ads: konto %s, %s: %s", account.pk, label, error.message)
                problems.append(f"{label}: {error.message}")
                return None

        step("Konverteringarna", lambda: google_conversions.ensure_conversion_actions(account))
        if google_conversions.upload_enabled():
            uploads = step(
                "Uppladdningen",
                lambda: google_conversions.upload_queued(account, validate_only=validate_only),
            )
            if uploads is not None and validate_only:
                parts.append(self._validated(account, uploads))
            elif uploads is not None:
                parts.append(
                    f"{uploads['sent']} konverteringar skickade, {uploads['failed']} "
                    f"med fel (står kvar i kö), {uploads['waiting']} väntar"
                )
        else:
            parts.append("konverteringarna går som CSV")
        if (
            not validate_only
            and google_conversions.verdicts_enabled()
            and google_conversions.awaiting_verdict(account).exists()
        ):
            checked = step("Googles besked", lambda: google_conversions.check_sent(account))
            if checked is not None and (checked["confirmed"] or checked["requeued"]):
                parts.append(
                    f"Googles besked: {checked['confirmed']} klara, "
                    f"{checked['requeued']} tillbaka i kö"
                )
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

    def _validated(self, account, uploads):
        """--prova: vad Google sa om raderna, en rad per nej. Inget skickades."""
        validated = uploads.get("validated", 0)
        invalid = uploads.get("invalid", [])
        text = (
            f"{validated} konverteringar godkända av Google, {len(invalid)} med fel, inget skickat"
        )
        self.stdout.write(f"  {account.customer.name}: {text}")
        for pk, message in invalid:
            self.stdout.write(f"    rad {pk}: {message}")
        return text

    @staticmethod
    def _record(account, message):
        FlamingoAccount.objects.filter(pk=account.pk).update(google_sync_error=message[:300])
