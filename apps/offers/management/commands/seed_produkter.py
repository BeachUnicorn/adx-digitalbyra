"""
Seeda produktkatalogen ur seed_data/adx_produkter.json.

ADDITIVT: en produkt som redan finns (matchad på namn) rörs aldrig -
priser och texter Giovanni satt i /manage/produkter/ är sanningskällan.
Riktpris 0 i seedfilen betyder "sätts per offert"; katalogen bär bara de
priser som faktiskt är fastställda, inget hittas på.

    manage.py seed_produkter            # skapar det som saknas
    manage.py seed_produkter --dry-run
"""

import json
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.offers.models import PricePeriod, Product

SEED_FILE = Path(django_settings.BASE_DIR) / "seed_data" / "adx_produkter.json"


class Command(BaseCommand):
    help = "Seedar produktkatalogen för offerter (additivt, skriver aldrig över)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not SEED_FILE.exists():
            raise CommandError(f"Hittar inte {SEED_FILE}.")
        rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))["produkter"]
        existing = set(Product.objects.values_list("name", flat=True))
        created = 0
        with transaction.atomic():
            for row in rows:
                if row["namn"] in existing:
                    continue
                if row["pristyp"] not in PricePeriod.values:
                    raise CommandError(f"Okänd pristyp för {row['namn']}: {row['pristyp']}")
                Product.objects.create(
                    name=row["namn"],
                    description=row.get("beskrivning", ""),
                    default_price=int(row.get("riktpris") or 0),
                    default_period=row["pristyp"],
                )
                created += 1
            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry-run: inget sparades."))
        self.stdout.write(
            self.style.SUCCESS(
                f"Klart. {created} produkter skapade ({len(rows) - created} fanns redan)."
            )
        )
