"""
Påminnelse en vecka före månadsskiftet: kunder med osända loggposter.

Körs dagligen från cron och gör bara något på rätt dag (sista dagen i
månaden minus sju), så att mejlet kommer en gång per månad. Skickar BARA
om något faktiskt är osänt. Själva sammanställningen till kunden går
aldrig härifrån - den skickas av knappen på kundens sida i panelen.

    manage.py remind_log_digest [--force]
"""

import calendar
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone

from apps.projects.emails import send_log_reminder
from apps.projects.models import Customer


def is_reminder_day(today):
    last = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    return today == last - timedelta(days=7)


class Command(BaseCommand):
    help = "Mejla byrån en påminnelse om osända loggposter en vecka före månadsskiftet."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Skicka oavsett datum.")

    def handle(self, *args, **options):
        today = timezone.localdate()
        if not options["force"] and not is_reminder_day(today):
            self.stdout.write("Inte påminnelsedag - inget mejl.")
            return
        rows = [
            (customer, customer.unsent)
            for customer in Customer.objects.annotate(
                unsent=Count("log_entries", filter=Q(log_entries__digest__isnull=True))
            ).filter(unsent__gt=0)
        ]
        if not rows:
            self.stdout.write("Inga osända loggposter - inget mejl.")
            return
        if send_log_reminder(rows):
            self.stdout.write(f"Påminnelse skickad för {len(rows)} kund(er).")
        else:
            self.stderr.write("Mejlet gick inte iväg (e-post inte konfigurerad?).")
