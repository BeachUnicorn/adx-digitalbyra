"""
Fill in map coordinates for serviceområden via Google's Geocoding API.

    manage.py geocode_areas                 # only areas that lack coordinates
    manage.py geocode_areas --all           # re-geocode everything
    manage.py geocode_areas --level district
    manage.py geocode_areas --dry-run

Run once after importing the areas. Geocoding is a separate, cheap SKU with its
own free monthly allowance, and 252 lookups is a one-off - the customer-facing
map is what costs money, and that is click-to-load.

The query is built from the area and its ancestors ("Råsunda, Solna, Sverige")
so that names which repeat around the country resolve to the right place, and
results are constrained to Sweden. Anything the API returns as approximate is
still stored - a municipality centre is exactly what we want on these pages -
but a lookup with no result is left empty rather than guessed at, so the page
renders without a map instead of with a wrong one.
"""

import json
import time
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.areas.models import Area, AreaLevel

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
#: Google asks for no more than ~50 requests/second; this is far below that and
#: keeps us polite on a one-off batch run.
DELAY_SECONDS = 0.12


class Command(BaseCommand):
    help = "Hämta koordinater för serviceområden via Google Geocoding API."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Geokoda även områden som redan har koordinater.",
        )
        parser.add_argument(
            "--level",
            choices=AreaLevel.values,
            help="Begränsa till en nivå (region, municipality, district).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Visa utan att spara.")

    def handle(self, *args, **options):
        key = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
        if not key:
            raise CommandError(
                "GOOGLE_MAPS_API_KEY är inte satt. Lägg till den i .env och försök igen."
            )

        areas = Area.objects.select_related("parent", "parent__parent").order_by("level", "name")
        if options["level"]:
            areas = areas.filter(level=options["level"])
        if not options["all"]:
            areas = areas.filter(latitude__isnull=True)

        total = areas.count()
        if not total:
            self.stdout.write("Inget att geokoda - alla områden har redan koordinater.")
            return

        self.stdout.write(f"Geokodar {total} områden...")
        found = missing = 0

        for index, area in enumerate(areas, start=1):
            query = self._query_for(area)
            try:
                lat, lng = self._geocode(query, key)
            except Exception as exc:  # noqa: BLE001 - report and continue the batch
                self.stderr.write(f"  {area.name}: fel ({exc})")
                missing += 1
                continue

            if lat is None:
                self.stderr.write(f"  {area.name}: ingen träff för '{query}'")
                missing += 1
            else:
                found += 1
                if not options["dry_run"]:
                    Area.objects.filter(pk=area.pk).update(latitude=lat, longitude=lng)
                if index % 25 == 0 or index == total:
                    self.stdout.write(f"  {index}/{total} klara")

            time.sleep(DELAY_SECONDS)

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry-run: inget sparades."))
        self.stdout.write(self.style.SUCCESS(f"Klart. {found} med koordinater, {missing} utan."))

    @staticmethod
    def _query_for(area):
        """Area name plus its ancestors, so repeated place names disambiguate."""
        parts = [area.name] + [node.name for node in reversed(area.ancestors())]
        # The län name is noise in a geocoding query once the kommun is there.
        parts = [p for p in parts if not p.endswith(" län")] or [area.name]
        return ", ".join(parts + ["Sverige"])

    @staticmethod
    def _geocode(query, key):
        params = urllib.parse.urlencode({"address": query, "components": "country:SE", "key": key})
        with urllib.request.urlopen(f"{GEOCODE_URL}?{params}", timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))

        status = payload.get("status")
        if status == "ZERO_RESULTS":
            return None, None
        if status != "OK":
            raise RuntimeError(payload.get("error_message") or status)

        location = payload["results"][0]["geometry"]["location"]
        return round(location["lat"], 6), round(location["lng"], 6)
