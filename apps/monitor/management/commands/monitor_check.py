"""
Övervakningens cron-ingång.

    manage.py monitor_check            # snabbkontroll var 5:e minut: drifttid, svarstid, endpoint
    manage.py monitor_check --daily    # dygnskontroll: cert, domän, e-post, säkerhet, Sentry,
                                       # PageSpeed och Googles data (CrUX, Search Console, GBP)
    manage.py monitor_check --domain nordanbygg.se [--daily] [--skip-slow]

Snabbkontrollen tittar också på utskickens tick (apps/utskick): har den inte
gått på fem minuter medan något väntar larmas byrån, högst en gång i timmen.
"""

import logging

from django.apps import apps
from django.core.management.base import BaseCommand

from apps.monitor.models import MonitoredDomain
from apps.monitor.runner import run_all


class Command(BaseCommand):
    help = "Kör övervakningens kontroller för alla aktiva domäner."

    def add_arguments(self, parser):
        parser.add_argument(
            "--daily", action="store_true", help="Dygnskontrollerna i stället för snabbkontrollen."
        )
        parser.add_argument("--domain", help="Bara den här domänen.")
        parser.add_argument("--skip-slow", action="store_true", help="Hoppa över PageSpeed.")

    def handle(self, *args, **options):
        domains = None
        if options["domain"]:
            domains = list(MonitoredDomain.objects.filter(name=options["domain"].strip().lower()))
            if not domains:
                self.stderr.write("Okänd domän.")
                return
        count, attention = run_all(
            daily=options["daily"], skip_slow=options["skip_slow"], domains=domains
        )
        self.stdout.write(f"{count} domän(er) kontrollerade.")
        for domain, text in attention:
            self.stdout.write(f"  {domain.name}: {text}")
        if not options["daily"] and domains is None:
            _utskick_heartbeat(self.stdout)


def _utskick_heartbeat(out):
    """Utskickens tick (apps/utskick/sending/tick.check_heartbeat). Ingenting
    när appen inte finns, och ett fel här fäller aldrig övervakningen."""
    if not apps.is_installed("apps.utskick"):
        return
    try:
        from apps.utskick.sending.tick import check_heartbeat

        if check_heartbeat():
            out.write("  Utskick: ticken har stannat, byrån är larmad.")
    except Exception:  # noqa: BLE001 - övervakningen ska alltid gå klart
        logging.getLogger(__name__).exception("Utskickens hjärtslag kunde inte kontrolleras")
