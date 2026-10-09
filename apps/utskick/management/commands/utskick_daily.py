"""
Utskickens dygnsstädning (README D.1, E.7), 02.45 från cron, före
sms_close_month 03.10 den första:

    manage.py utskick_daily

Räknare äldre än två dygn, importernas filer och gamla jobb, händelser
efter 25 månader, samtyckeslogg för borttagna kontakter efter 36 månader,
exportloggen efter 25 månader, flaggan på inaktiva kontakter, diskens
lediga utrymme (larm till byrån under 15 %) och tabellernas storlek.
Allt i retention.daily; raden här bär bara antal. Senare steg lägger till
SES-kontrollerna, köerna och månadsskiftets avstämning.
"""

import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.utskick import retention


class Command(BaseCommand):
    help = "Utskickens dygnsstädning (E.7): retention, importfiler, inaktiva kontakter, disk."

    def handle(self, *args, **options):
        now = timezone.now()
        summary = retention.daily(now)
        stamp = timezone.localtime(now).strftime("%Y-%m-%d %H:%M:%S")
        self.stdout.write(f"{stamp} utskick_daily {json.dumps(summary, ensure_ascii=False)}")
