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
    ("Förvaltning och drift", "forvaltning"),
]

#: Branschkolumnen. Sidfoten renderas på VARJE sida, så den här kolumnen är
#: det som binder ihop sökordssidorna med ortssidorna - två silor som annars
#: inte länkar till varandra alls. Ortssidan renderar samma lista som en egen
#: sektion (se areas.views.INDUSTRY_MENU_HEADING), så listan finns på ett
#: ställe och inte två.
INDUSTRY_HEADING = "Branscher"
INDUSTRY_LINKS = [
    ("Bygg och hantverk", "hemsida-byggforetag"),
    ("VVS och el", "hemsida-vvs"),
    ("Tandvård och klinik", "hemsida-tandlakare"),
    ("Juridik och ekonomi", "hemsida-advokatbyra"),
    ("Restaurang och hotell", "hemsida-restaurang"),
    ("Konsult och IT", "hemsida-konsultbolag"),
]

#: Sidfotslänken till ortssidorna. Den seedades som en rå adress och pekade
#: kvar på /digitalbyra/ efter sökordsbytet, eftersom seed_site aldrig körs om
#: i produktion. Resultatet: enda vägen in till 109 ortssidor var en länk som
#: dessutom gick via en redirect.
AREAS_URL_OLD = "/digitalbyra/"
AREAS_URL = "/webbyra/"
AREAS_LABEL = "Webbyrå i din stad"


class Command(BaseCommand):
    help = "Seedar sökordssidor + deras FAQ ur seed_data/ (additivt, tar aldrig bort)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Visa vad som skulle skapas utan att spara.",
        )
        parser.add_argument(
            "--file",
            default=str(SEED_FILE),
            help="Seedfil i samma format (standard: adx_sokordssidor.json).",
        )

    def handle(self, *args, **options):
        seed_file = Path(options["file"])
        if not seed_file.exists():
            raise CommandError(f"Hittar inte {seed_file}.")
        payload = json.loads(seed_file.read_text(encoding="utf-8"))

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
                    # Undantaget är länkmotorns kategori - ren metadata som
                    # inte syns i något innehåll, och utan den står sidan
                    # utanför ringen (apps/website/related.py). Bara tomma
                    # fylls i; en satt kategori är kundens.
                    kategori = entry.get("kategori", "")
                    if kategori and not page.category:
                        page.category = kategori
                        page.save(update_fields=["category", "updated_at"])
                    skipped_pages += 1
                    continue

                page = BlockPage.objects.create(
                    slug=entry["slug"],
                    title=entry["titel"],
                    meta_title=entry["meta_title"],
                    meta_description=entry["meta_description"],
                    gradient_color=entry["gradient"],
                    category=entry.get("kategori", ""),
                    is_published=True,
                    order=50,
                )
                created_pages += 1
                created_blocks += self._blocks(page, entry["block"], section)

            created_menu = self._footer_menu()
            created_menu = self._file_footer(payload.get("sidfot") or []) or created_menu
            self._parent_blocks(payload.get("foraldrar") or {})
            self._retire_columns(payload.get("pensionera_kolumner") or [])
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
        """
        Lägg till sidfotskolumner OCH enskilda rader som saknas.

        Additivt på radnivå, inte bara kolumnnivå: när en ny länk läggs i
        listorna här måste den nå en sidfot som redan seedats en gång.
        Matchning sker på sidan (page-FK), så en rad kunden döpt om läggs
        inte till igen, och befintliga rader rörs aldrig.
        """
        added = False
        for heading, links in ((FOOTER_HEADING, FOOTER_LINKS), (INDUSTRY_HEADING, INDUSTRY_LINKS)):
            menu = Menu.objects.filter(location="footer", heading=heading).first()
            if menu is None:
                last = Menu.objects.filter(location="footer").order_by("-order").first()
                menu = Menu.objects.create(
                    location="footer",
                    name=f"Sidfot: {heading}",
                    heading=heading,
                    order=(last.order + 1) if last else 0,
                )
            existing_page_ids = set(
                menu.items.filter(page__isnull=False).values_list("page_id", flat=True)
            )
            last_item = menu.items.order_by("-order").first()
            next_order = (last_item.order + 1) if last_item else 0
            for label, slug in links:
                page = BlockPage.objects.filter(slug=slug).first()
                if page is None or page.pk in existing_page_ids:
                    continue
                MenuItem.objects.create(menu=menu, label=label, page=page, order=next_order)
                next_order += 1
                added = True
        self._fix_areas_link()
        return added

    def _file_footer(self, columns):
        """
        Sidfotskolumner definierade i seedfilen: [{"rubrik", "lankar": [[etikett,
        slug-eller-adress], ...]}]. Samma radvisa additivitet som kodens
        kolumner - befintliga rader rörs aldrig, saknade läggs till.
        """
        added = False
        for spec in columns:
            heading = spec["rubrik"]
            menu = Menu.objects.filter(location="footer", heading=heading).first()
            if menu is None:
                last = Menu.objects.filter(location="footer").order_by("-order").first()
                menu = Menu.objects.create(
                    location="footer",
                    name=f"Sidfot: {heading}",
                    heading=heading,
                    order=(last.order + 1) if last else 0,
                )
            existing_pages = set(
                menu.items.filter(page__isnull=False).values_list("page_id", flat=True)
            )
            existing_urls = set(menu.items.exclude(url="").values_list("url", flat=True))
            last_item = menu.items.order_by("-order").first()
            next_order = (last_item.order + 1) if last_item else 0
            for label, target in spec["lankar"]:
                if target.startswith("/"):
                    if target in existing_urls:
                        continue
                    MenuItem.objects.create(menu=menu, label=label, url=target, order=next_order)
                else:
                    page = BlockPage.objects.filter(slug=target).first()
                    if page is None or page.pk in existing_pages:
                        continue
                    MenuItem.objects.create(menu=menu, label=label, page=page, order=next_order)
                next_order += 1
                added = True
        return added

    def _parent_blocks(self, parents):
        """
        Undersidornas väg in: ett related-block på föräldrasidan som listar
        barnen. {"domain": {"rubrik": "...", "barn": ["slug", ...]}}. Blocket
        skapas före bar-blocket om det saknas; finns det läggs bara saknade
        länkar till (matchat på sida), aldrig något borttaget.
        """
        for parent_slug, spec in parents.items():
            parent = BlockPage.objects.filter(slug=parent_slug).first()
            if parent is None:
                continue
            block = (
                parent.blocks.filter(block_type="related", data__title=spec["rubrik"]).first()
            )
            if block is None:
                bar = parent.blocks.filter(block_type="bar").order_by("-order").first()
                if bar is not None:
                    order = bar.order
                    bar.order += 1
                    bar.save(update_fields=["order", "updated_at"])
                else:
                    last = parent.blocks.order_by("-order").first()
                    order = (last.order + 1) if last else 1
                block = Block.objects.create(
                    page=parent,
                    block_type="related",
                    data=clean_block_values(
                        "related", {}, {"kicker": "Tjänster", "title": spec["rubrik"]}
                    ),
                    order=order,
                    is_visible=True,
                )
            links = list(block.data.get("links") or [])
            linked_ids = {
                link["url"]["id"]
                for link in links
                if isinstance(link.get("url"), dict) and link["url"].get("kind") == "page"
            }
            changed = False
            for slug in spec["barn"]:
                child = BlockPage.objects.filter(slug=slug).first()
                if child is None or child.pk in linked_ids:
                    continue
                links.append({"label": child.title, "url": {"kind": "page", "id": child.pk}})
                changed = True
            if changed:
                block.data["links"] = links
                block.save(update_fields=["data", "updated_at"])

    def _retire_columns(self, specs):
        """
        Ta bort en sidfotskolumn BARA om den är exakt som seedad ({"rubrik",
        "sidor": [slugs]}). Har kunden rört den (annan rad, annan ordning)
        lämnas den orörd - additivitetsregeln gäller även här.
        """
        for spec in specs:
            menu = Menu.objects.filter(location="footer", heading=spec["rubrik"]).first()
            if menu is None:
                continue
            actual = [
                item.page.slug if item.page_id else item.url
                for item in menu.items.order_by("order", "id")
            ]
            if actual == spec["sidor"]:
                menu.delete()

    def _fix_areas_link(self):
        """
        Peka sidfotens ortslänk på /webbyra/ i stället för gamla /digitalbyra/.

        Menyposten bär en rå adress (ortsöversikten är ingen BlockPage och kan
        därför inte vara en sid-FK). Sökordsbytet uppdaterade koden men inte
        raden i databasen, så länken gick via en 301 på varje sidvisning.
        """
        for item in MenuItem.objects.filter(url=AREAS_URL_OLD):
            item.url = AREAS_URL
            item.label = AREAS_LABEL
            item.save(update_fields=["url", "label"])

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
