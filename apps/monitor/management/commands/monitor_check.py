"""
Övervakningens cron-ingång.

    manage.py monitor_check            # snabbkontroll var 5:e minut: drifttid, svarstid, endpoint
    manage.py monitor_check --daily    # dygnskontroll: cert, domän, e-post, säkerhet, Sentry
    manage.py monitor_check --domain nordanbygg.se [--daily] [--skip-slow]
"""

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
