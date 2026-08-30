"""
Länkrapport: hittar föräldralösa sidor genom att crawla den egna sajten.

En sida i sitemapen som ingen intern länk pekar på blir i praktiken
osynlig - Google värderar sidor efter inlänkar, och besökare hittar dem
aldrig. Mätningen 2026-08-30 hittade två sådana; ringmotorn
(apps/website/related.py) ska göra dem omöjliga, och det här kommandot
är kvittot.

Crawlen går via testklienten mot den egna databasen, så den fungerar
likadant lokalt och i produktion och belastar aldrig nätet:

    manage.py lankrapport            # rapport med inlänksräkning
    manage.py lankrapport --fail     # felstatus om föräldralösa finns
                                     # (för deploykedjan)

Startsidan undantas från bevakningen - logotypen länkar dit från varje
sida, så den kan inte bli föräldralös på något meningsfullt sätt.
"""

import re
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

HREF_RE = re.compile(r'href="(/[^"#?]*)"')
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")


class Command(BaseCommand):
    help = "Crawlar sajten och rapporterar sidor utan interna inlänkar."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fail",
            action="store_true",
            help="Avsluta med felstatus om föräldralösa sidor hittas.",
        )

    def handle(self, *args, **options):
        # Testklientens standardhost "testserver" avvisas av produktionens
        # ALLOWED_HOSTS, och secure=True undviker SSL-redirecten där.
        host = next(
            (h.lstrip(".") for h in settings.ALLOWED_HOSTS if h and "*" not in h),
            "testserver",
        )
        client = Client(HTTP_HOST=host)
        response = client.get("/sitemap.xml", secure=True)
        if response.status_code != 200:
            raise CommandError(f"sitemap.xml svarade {response.status_code}")

        paths = [
            re.sub(r"https?://[^/]+", "", url)
            for url in LOC_RE.findall(response.content.decode())
        ]
        inbound = Counter()
        broken = []
        for path in paths:
            page = client.get(path, secure=True)
            if page.status_code != 200:
                broken.append((path, page.status_code))
                continue
            html = page.content.decode()
            for href in set(HREF_RE.findall(html)):
                if href != path:
                    inbound[href] += 1

        orphans = [p for p in paths if p != "/" and inbound[p] == 0]

        self.stdout.write(f"{len(paths)} sidor i sitemapen.")
        for path, status in broken:
            self.stdout.write(self.style.ERROR(f"  {path} svarade {status}"))
        if orphans:
            self.stdout.write(self.style.ERROR(f"{len(orphans)} föräldralösa sidor:"))
            for path in orphans:
                self.stdout.write(f"  {path}")
        else:
            self.stdout.write(self.style.SUCCESS("Inga föräldralösa sidor."))

        least = sorted((inbound[p], p) for p in paths if p != "/")[:5]
        self.stdout.write("Färst inlänkar:")
        for count, path in least:
            self.stdout.write(f"  {count:>3}  {path}")

        if options["fail"] and (orphans or broken):
            raise CommandError("Länkrapporten hittade problem (se ovan).")
