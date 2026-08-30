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
import shutil
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.faq.models import FAQItem, FAQSection
from apps.manage.block_schema import BLOCK_EDIT_SCHEMA, clean_block_rows, clean_block_values
from apps.website.models import Block, BlockPage, MediaFile, Menu, MenuItem

SEED_FILE = Path(django_settings.BASE_DIR) / "seed_data" / "adx_sokordssidor.json"
MEDIA_SEED_DIR = Path(django_settings.BASE_DIR) / "seed_data" / "media"

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
        self._media_cache = {}
        # Alt-texter och portfoliokopplingar är INNEHÅLL och bor i seedfilen.
        # Koden ska inte bära kundnamn - identitetsvakten i
        # apps/website/tests.py grep-ar efter just det.
        self._images = payload.get("bilder") or {}

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
            self._portfolio_image()

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

    def _portfolio_image(self):
        """
        Fyll i bild och länk på ett portfoliokort som fortfarande är tomt.

        Portfoliokorten seedades utan bild och utan länk, vilket gör dem till
        svag bevisning - ett case utan bild och utan väg vidare övertygar
        ingen. Vilket kort som ska fyllas står i seedfilen, inte här.
        Ett kort kunden själv fyllt i skrivs aldrig över.
        """
        for name, spec in self._images.items():
            card_spec = spec.get("portfoliokort")
            if not card_spec:
                continue
            needle = card_spec["titel_innehaller"].lower()
            target = BlockPage.objects.filter(slug=card_spec["lanka_till"]).first()
            for block in Block.objects.filter(
                block_type="folio", page__slug=card_spec["sida"]
            ):
                cards = block.data.get("cards") or []
                changed = False
                for card in cards:
                    if needle not in str(card.get("title", "")).lower():
                        continue
                    if not card.get("image_id"):
                        card["image_id"] = self.media_file(name).pk
                        changed = True
                    if not card.get("url") and target is not None:
                        card["url"] = {"kind": "page", "id": target.pk}
                        changed = True
                if changed:
                    block.data["cards"] = cards
                    block.save(update_fields=["data", "updated_at"])

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

    def media_file(self, name):
        """
        MediaFile för en bild som ligger i seed_data/media/. Idempotent.

        Bilden kopieras till MEDIA_ROOT precis som import_site_data gör, så att
        seeden fungerar likadant på en server som lokalt.
        """
        if name in self._media_cache:
            return self._media_cache[name]

        src = next(MEDIA_SEED_DIR.glob(f"{name}.*"), None)
        if src is None:
            raise CommandError(f"Hittar inte bilden seed_data/media/{name}.*")

        rel = f"media/{src.name}"
        existing = MediaFile.objects.filter(file=rel).first()
        if existing is None:
            dst = Path(django_settings.MEDIA_ROOT) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            width = height = None
            try:
                from PIL import Image

                with Image.open(src) as im:
                    width, height = im.size
            except Exception:  # noqa: BLE001 - måtten är trevliga, inte kritiska
                pass
            existing = MediaFile.objects.create(
                file=rel,
                original_filename=src.name,
                alt_text=(self._images.get(name) or {}).get("alt", ""),
                mime_type="image/jpeg" if src.suffix in (".jpg", ".jpeg") else "image/png",
                file_size=src.stat().st_size,
                width=width,
                height=height,
            )
        self._media_cache[name] = existing
        return existing

    def _blocks(self, page, specs, section):
        for order, spec in enumerate(specs, start=1):
            block_type = spec["typ"]
            if block_type not in BLOCK_EDIT_SCHEMA:
                raise CommandError(f"Okänd blocktyp i seedfilen: {block_type}")

            falt = dict(spec.get("falt") or {})
            # FAQ-blocket bär sektionens slug i filen; id:t finns först nu.
            if "faq_section_slug" in spec:
                falt["faq_section_id"] = str(section.pk)
            # Bilder bärs som filnamn i seedfilen - MediaFile-id:t finns
            # först när bilden importerats.
            for key, image_name in (spec.get("bild") or {}).items():
                falt[key] = str(self.media_file(image_name).pk)

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
