"""
Konverteringsinnehåll: formulär på pengasidorna, portfoliostädning, case
vid kontaktformuläret.

Tre saker, alla beslutade 2026-08-30:

1. Förfrågningsformuläret fanns bara på /kontakt/ - varje sökords- och
   branschsida slutade i en länk dit, ett extra klick som kostar leads.
   Nu läggs ett inquiry_form-block sist (före bar-blocket) på varje sida
   ur seed_data/adx_sokordssidor.json som saknar ett.
2. De tomma portfoliokorten (utan bild OCH utan länk) tas bort ur
   folio-blocken - ett tomt kort signalerar "ny byrå utan kunder" precis
   där besökaren letar bevis.
3. Kontaktsidan får senaste caset klonat ovanför formuläret - besökaren som
   tvekar vid formuläret gör en sista trovärdighetskoll.

ADDITIVT och idempotent i formulärsdelen (sidor som redan har formulär
rörs inte). Kortstädningen tar bara bort kort som är bevisat tomma.
"""

import json
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.manage.block_schema import clean_block_values
from apps.website.models import Block, BlockPage

SEED_FILE = Path(django_settings.BASE_DIR) / "seed_data" / "adx_sokordssidor.json"

FORM_FALT = {
    "kicker": "Förfrågan",
    "title": "Berätta vad ni behöver",
    "intro": "Svar inom en arbetsdag. Första mötet kostar ingenting.",
}


class Command(BaseCommand):
    help = "Formulär på pengasidorna, städade portfoliokort, case på kontaktsidan."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        forms_added = cards_removed = case_added = 0

        with transaction.atomic():
            forms_added = self._add_forms()
            cards_removed = self._trim_folio_cards()
            case_added = int(self._case_on_kontakt())
            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry-run: inget sparades."))

        self.stdout.write(
            self.style.SUCCESS(
                f"Klart. {forms_added} formulär tillagda, {cards_removed} tomma "
                f"portfoliokort borttagna, case på kontaktsidan: "
                f"{'ja' if case_added else 'fanns redan'}."
            )
        )

    def _add_forms(self):
        slugs = [s["slug"] for s in json.loads(SEED_FILE.read_text())["sidor"]]
        added = 0
        for page in BlockPage.objects.filter(slug__in=slugs):
            if page.blocks.filter(block_type="inquiry_form").exists():
                continue
            bar = page.blocks.filter(block_type="bar").order_by("-order").first()
            if bar is not None:
                order = bar.order
                bar.order += 1
                bar.save(update_fields=["order", "updated_at"])
            else:
                last = page.blocks.order_by("-order").first()
                order = (last.order + 1) if last else 1
            Block.objects.create(
                page=page,
                block_type="inquiry_form",
                data=clean_block_values("inquiry_form", {}, FORM_FALT),
                order=order,
                is_visible=True,
            )
            added += 1
        return added

    def _trim_folio_cards(self):
        removed = 0
        for block in Block.objects.filter(block_type="folio"):
            cards = block.data.get("cards") or []
            kept = [c for c in cards if c.get("image_id") or c.get("url")]
            if len(kept) != len(cards):
                removed += len(cards) - len(kept)
                block.data["cards"] = kept
                block.save(update_fields=["data", "updated_at"])
        return removed

    def _case_on_kontakt(self):
        """Klona casesidans case-block till kontaktsidan, före formuläret.

        Innehållet KOPIERAS från en publicerad case-sida (slug "case-*") i
        stället för att ligga i koden - identitetsvakten förbjuder kundnamn
        i kodbasen, och casesidan är redan sanningskällan. Länken pekas om
        till casesidan så kortet blir en väg dit.
        """
        page = BlockPage.objects.filter(slug="kontakt").first()
        if page is None or page.blocks.filter(block_type="case").exists():
            return False
        form = page.blocks.filter(block_type="inquiry_form").order_by("order").first()
        if form is None:
            return False
        case_page = (
            BlockPage.objects.filter(slug__startswith="case-", is_published=True)
            .order_by("order", "slug")
            .first()
        )
        if case_page is None:
            return False
        source = case_page.blocks.filter(block_type="case").order_by("order").first()
        if source is None:
            return False

        import copy

        data = copy.deepcopy(source.data)
        data["kicker"] = "Senaste leveransen"
        data["title"] = "Byggt av oss"
        link = data.get("link") or {}
        link["label"] = "Läs hela caset"
        link["url"] = {"kind": "page", "id": case_page.pk}
        data["link"] = link

        # In före formuläret: trovärdighetskollen ska ske innan man skriver.
        for later in page.blocks.filter(order__gte=form.order).order_by("-order"):
            later.order += 1
            later.save(update_fields=["order", "updated_at"])
        Block.objects.create(
            page=page, block_type="case", data=data, order=form.order, is_visible=True
        )
        return True
