"""
Utskickens tick (README D.1, D.2), varje minut från cron:

    manage.py utskick_tick                # en tick med UTSKICK_TICK_SECONDS
    manage.py utskick_tick --budget 20    # kortare budget (manuellt)
    manage.py utskick_tick --verbose      # skriv raden även när inget gjordes
    manage.py utskick_tick --only 41      # bara utskick 41 (frysning, sms, klart)

Cron-raden (server/crontab.d/adx-utskick) kör den under flock -n och choom,
så att två tickar aldrig körs samtidigt och ticken är den OOM-mördaren tar
först. Första som händer här är minnestaket (RLIMIT_AS, UTSKICK_TICK_MAX_MB;
0 stänger av det, som testerna gör). Själva ticken bor i sending/tick.py.

En rad skrivs till backups/utskick.log bara när ticken gjorde något: antal,
aldrig adresser.
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.utskick.sending import tick

logger = logging.getLogger(__name__)


def limit_memory():
    """RLIMIT_AS för den här processen (UTSKICK_TICK_MAX_MB). macOS vägrar
    ibland sänka det; då körs ticken utan tak (bara lokalt)."""
    mb = int(getattr(settings, "UTSKICK_TICK_MAX_MB", 0) or 0)
    if mb <= 0:
        return False
    try:
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = mb * 1024 * 1024
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ImportError, ValueError, OSError):
        logger.info("utskick_tick: minnestaket kunde inte sättas här")
        return False
    return True


class Command(BaseCommand):
    help = "Utskickens tick: utskick, sms, bekräftelser och importer i bakgrunden (varje minut)."

    def add_arguments(self, parser):
        parser.add_argument("--budget", type=int, help="Sekunder (standard UTSKICK_TICK_SECONDS).")
        parser.add_argument(
            "--verbose", action="store_true", help="Skriv raden även när inget gjordes."
        )
        parser.add_argument("--only", type=int, help="Bara det här utskicket (pk), manuellt.")

    def handle(self, *args, **options):
        limit_memory()
        summary = tick.run(budget=options["budget"], only=options.get("only"))
        if summary.get("status") == "worked" or options["verbose"]:
            self.stdout.write(tick.summary_line(summary))
