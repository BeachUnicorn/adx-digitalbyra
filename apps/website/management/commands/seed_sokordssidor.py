"""
Seeda sökordssidorna ur seed_data/adx_sokordssidor.json.

ADDITIVT, till skillnad från seed_site: en sida som redan finns lämnas orörd,
och inga block eller frågor tas bort. Det gör kommandot säkert att köra på en
produktion där kunden hunnit redigera - efter första seeden är produktionen
sanningskällan, precis som för allt annat innehåll.

    manage.py seed_sokordssidor              # skapar det som saknas
    manage.py seed_sokordssidor --dry-run    # visar vad som skulle skapas

Blockens innehåll går genom SAMMA sanerare som AI-vägen och /manage/-formulären
(clean_block_values / clean_block_rows). En seedfil är en lika otrodd källa som
allt annat - och det är där länkar blir riktiga sid-referenser i stället för
råa strängar.
"""

import json
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.faq.models import FAQItem, FAQSection
from apps.manage.block_schema import BLOCK_EDIT_SCHEMA, clean_block_rows, clean_block_values
from apps.website.models import Block, BlockPage, Menu, MenuItem

SEED_FILE = Path(django_settings.BASE_DIR) / "seed_data" / "adx_sokordssidor.json"

#: Sidfotskolumnen som ger sidorna en väg in. Utan den är de föräldralösa:
#: de finns i sitemapen och länkar till varandra, men ingenting på sajten
#: leder dit, och interna länkar är en stor del av varför en sida rankar.
#: Posterna refererar SIDOR (page-FK), aldrig adresser - länkregeln.
FOOTER_HEADING = "Hemsida"
FOOTER_LINKS = [
    ("Skapa hemsida", "skapa-hemsida"),
    ("Vad kostar en hemsida?", "vad-kostar-en-hemsida"),
    ("Billig hemsida", "billig-hemsida"),
    ("Hemsida för företag", "hemsida-foretag"),
]


class Command(BaseCommand):
    help = "Seedar sökordssidor + deras FAQ ur seed_data/ (additivt, tar aldrig bort)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Visa vad som skulle skapas utan att spara.",
        )

    def handle(self, *args, **options):
        if not SEED_FILE.exists():
            raise CommandError(f"Hittar inte {SEED_FILE}.")
        payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))

        created_pages = created_blocks = created_sections = created_items = 0
        skipped_pages = 0

        with transaction.atomic():
            for entry in payload["sidor"]:
                section, made = self._faq_section(entry["faq"])
                created_sections += int(made)
                created_items += self._faq_items(section, entry["faq"]["fragor"])

                page = BlockPage.objects.filter(slug=entry["slug"]).first()
                if page is not None:
                    # Sidan finns: rör den inte. Kunden kan ha skrivit om den.
                    skipped_pages += 1
                    continue

                page = BlockPage.objects.create(
                    slug=entry["slug"],
                    title=entry["titel"],
                    meta_title=entry["meta_title"],
                    meta_description=entry["meta_description"],
                    gradient_color=entry["gradient"],
                    is_published=True,
                    order=50,
                )
                created_pages += 1
                created_blocks += self._blocks(page, entry["block"], section)

            created_menu = self._footer_menu()

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry-run: inget sparades."))

        if not options["dry_run"] and created_pages:
            # Seeden skriver rått och passerar aldrig modellernas save-sanering;
            # samma efterstädning som seed_site gör.
            call_command("normalize_typography", verbosity=0)

        self.stdout.write(
            self.style.SUCCESS(
                f"Klart. {created_pages} sidor skapade ({skipped_pages} fanns redan), "
                f"{created_blocks} block, {created_sections} FAQ-sektioner, "
                f"{created_items} frågor"
                + (", sidfotskolumn tillagd." if created_menu else ".")
            )
        )

    # ------------------------------------------------------------------

    def _footer_menu(self):
        """Lägg till sidfotskolumnen om den inte redan finns. Rör inget annat."""
        if Menu.objects.filter(location="footer", heading=FOOTER_HEADING).exists():
            return False
        last = Menu.objects.filter(location="footer").order_by("-order").first()
        menu = Menu.objects.create(
            location="footer",
            name=f"Sidfot: {FOOTER_HEADING}",
            heading=FOOTER_HEADING,
            order=(last.order + 1) if last else 0,
        )
        for order, (label, slug) in enumerate(FOOTER_LINKS):
            page = BlockPage.objects.filter(slug=slug).first()
            if page is None:
                continue
            MenuItem.objects.create(menu=menu, label=label, page=page, order=order)
        return True

    def _faq_section(self, spec):
        section = FAQSection.objects.filter(slug=spec["slug"]).first()
        if section is not None:
            return section, False
        return (
            FAQSection.objects.create(
                slug=spec["slug"],
                title=spec["titel"],
                meta_description=spec.get("meta_description", ""),
                is_active=True,
            ),
            True,
        )

    def _faq_items(self, section, fragor):
        """Lägger till frågor som saknas. Befintliga frågor rörs aldrig."""
        existing = set(section.items.values_list("question", flat=True))
        added = 0
        for order, row in enumerate(fragor):
            if row["fraga"] in existing:
                continue
            FAQItem.objects.create(
                section=section,
                question=row["fraga"],
                answer=row["svar"],
                is_active=True,
                order=order,
            )
            added += 1
        return added

    def _blocks(self, page, specs, section):
        for order, spec in enumerate(specs, start=1):
            block_type = spec["typ"]
            if block_type not in BLOCK_EDIT_SCHEMA:
                raise CommandError(f"Okänd blocktyp i seedfilen: {block_type}")

            falt = dict(spec.get("falt") or {})
            # FAQ-blocket bär sektionens slug i filen; id:t finns först nu.
            if "faq_section_slug" in spec:
                falt["faq_section_id"] = str(section.pk)

            data = clean_block_values(block_type, {}, falt)
            if spec.get("listor"):
                data = clean_block_rows(block_type, data, spec["listor"])

            Block.objects.create(
                page=page,
                block_type=block_type,
                data=data,
                order=order,
                is_visible=True,
            )
        return len(specs)
