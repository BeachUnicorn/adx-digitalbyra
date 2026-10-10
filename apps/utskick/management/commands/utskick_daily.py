"""
Utskickens dygnsstädning (README D.1, E.7), 02.45 från cron, före
sms_close_month 03.10 den första:

    manage.py utskick_daily

Räknare äldre än två dygn, importernas filer och gamla jobb, händelser
efter 25 månader, samtyckeslogg för borttagna kontakter efter 36 månader,
exportloggen efter 25 månader, flaggan på inaktiva kontakter, diskens
lediga utrymme (larm till byrån under 15 %) och tabellernas storlek.
Allt i retention.daily; raden här bär bara antal. Från S3 också
SES-kontrollerna (GetAccount), domänernas DNS, DLQ:erna, hinken för svar och
oklara mejl (retention.s3_email_steps).

    manage.py utskick_daily --only ses    bara GetAccount (J S3 steg 6: fyller
                                          Switchboard.ses_max_rate och ses_daily_quota)
    manage.py utskick_daily --only domains | dlq   bara den delen
"""

import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.utskick import retention


class Command(BaseCommand):
    help = "Utskickens dygnsstädning (E.7): retention, importfiler, inaktiva kontakter, disk."

    def add_arguments(self, parser):
        # S3 (sändnings-byggaren): en del i taget, för checklistan.
        parser.add_argument(
            "--only",
            choices=("ses", "domains", "dlq"),
            help="Bara den delen (ses: SES GetAccount, J S3 steg 6).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        only = options.get("only")
        if only:
            names = {"ses": "ses", "domains": "domains", "dlq": "dlq"}
            steps = dict(retention.s3_email_steps(now))
            summary = {only: steps[names[only]]()}
        else:
            summary = retention.daily(now)
        stamp = timezone.localtime(now).strftime("%Y-%m-%d %H:%M:%S")
        self.stdout.write(f"{stamp} utskick_daily {json.dumps(summary, ensure_ascii=False)}")
