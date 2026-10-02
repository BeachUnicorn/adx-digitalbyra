"""
Seedar ADX Flamingos sidor ur seed_data/flamingo_pages.json.

Sidorna är vanliga blocksidor med design="flamingo" och redigeras i
/manage/ som alla andra; seeden ger bara ett reproducerbart första
innehåll. Kommandot är idempotent:

  * sidan hittas på sin slug och får titel och meta ur filen,
  * blocken ersätts deterministiskt (inte append),
  * varje blocks data går genom samma sanerare som /manage/-redigeraren
    (clean_block_values för fälten, clean_block_rows för radlistorna), så
    texten saneras och länkarna blir beskrivare precis som vid ett vanligt
    sparande ("/flamingo/app/" blir {"kind": "path", ...}),
  * ett faq-block med "faq_verbatim" blir en riktig FAQ-sektion (samma
    grepp som seed_site). Sektionen visas aldrig på de publika FAQ-sidorna,
    se apps/faq/visibility.py.

Publiceringen rörs aldrig på en befintlig sida. En ny sida skapas som
utkast: bara byrån ser den tills någon publicerar den i /manage/.

Körs för hand, aldrig av deploy:

    uv run python manage.py seed_flamingo
"""

import copy
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.common.security import sanitize_plain_text, sanitize_rich_html_basic
from apps.faq.models import FAQItem, FAQSection
from apps.manage.block_schema import (
    BLOCK_EDIT_SCHEMA,
    clean_block_rows,
    clean_block_values,
    types_for_design,
)
from apps.website.models import Block, BlockPage

SEED_FILE = Path(settings.BASE_DIR) / "seed_data" / "flamingo_pages.json"

_MISSING = object()


class Command(BaseCommand):
    help = "Seedar ADX Flamingos sidor ur seed_data/flamingo_pages.json (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=str(SEED_FILE),
            help="Seedfilen (standard: seed_data/flamingo_pages.json).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(options["file"])
        try:
            pages = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CommandError(f"Kunde inte läsa {path}: {exc}") from exc
        if not isinstance(pages, list) or not pages:
            raise CommandError(f"{path} ska vara en lista med minst en sida.")

        for spec in pages:
            page, created, count = self._seed_page(spec)
            state = "ny, opublicerad" if created else "publiceringen orörd"
            self.stdout.write(
                self.style.SUCCESS(f"{page.get_absolute_url()}: {count} block ({state}).")
            )

    # ------------------------------------------------------------------

    def _seed_page(self, spec):
        slug = spec.get("sida") or ""
        design = spec.get("design", BlockPage.DESIGN_FLAMINGO)
        if not slug:
            raise CommandError("En sida saknar 'sida' (sluggen).")
        if design != BlockPage.DESIGN_FLAMINGO:
            raise CommandError(f'"{slug}": seed_flamingo seedar bara Flamingo-sidor.')

        clash = BlockPage.objects.filter(slug=slug).exclude(design=design).first()
        if clash is not None:
            raise CommandError(f'Sluggen "{slug}" används redan av ADX-sidan "{clash.title}".')

        allowed = set(types_for_design(design))
        blocks = spec.get("blocks") or []
        for i, block in enumerate(blocks):
            block_type = block.get("type")
            if block_type not in allowed:
                raise CommandError(
                    f'"{slug}", block {i}: typen "{block_type}" finns inte i Flamingo-designen.'
                )

        fields = {
            "title": sanitize_plain_text(spec.get("title") or slug, max_length=255),
            "meta_title": sanitize_plain_text(spec.get("meta_title", ""), max_length=255),
            "meta_description": sanitize_plain_text(
                spec.get("meta_description", ""), max_length=300
            ),
        }
        page, created = BlockPage.objects.update_or_create(
            slug=slug,
            design=design,
            defaults=fields,
            create_defaults={**fields, "is_published": False},
        )

        # Deterministisk ersättning, inte append.
        page.blocks.all().delete()
        for order, block in enumerate(blocks):
            raw = copy.deepcopy(block.get("data") or {})
            if block["type"] == "faq":
                raw = self._seed_faq(page, raw)
            Block.objects.create(
                page=page,
                block_type=block["type"],
                data=self._clean(slug, order, block["type"], raw),
                order=order,
                is_visible=True,
            )
        return page, created, len(blocks)

    def _seed_faq(self, page, data):
        """faq_verbatim -> en FAQ-sektion med sanerade frågor och svar, och
        blockets faq_section_id pekar på den."""
        verbatim = data.pop("faq_verbatim", None)
        if not verbatim:
            return data
        slug = "flamingo-fragor"
        if page.slug != BlockPage.FLAMINGO_HOME_SLUG:
            slug = f"flamingo-{page.slug}-fragor"
        section, _ = FAQSection.objects.update_or_create(
            slug=slug,
            defaults={
                "title": sanitize_plain_text(f"{page.title}: frågor och svar", max_length=200),
                "is_active": True,
                # Aldrig publik: Flamingos frågor visas bara på Flamingo-sidor.
                "design": "flamingo",
            },
        )
        section.items.all().delete()
        for order, qa in enumerate(verbatim):
            FAQItem.objects.create(
                section=section,
                question=sanitize_plain_text(qa["q"], max_length=500),
                answer=sanitize_rich_html_basic(f"<p>{qa['a']}</p>"),
                order=order,
                is_active=True,
            )
        data["faq_section_id"] = section.pk
        return data

    def _clean(self, slug, order, block_type, raw):
        """Bygg om blockets data som redigeraren gör: varje deklarerat fält
        (tomt om det saknas i filen) och varje radlista, genom schemats
        sanerare. Okända nycklar är ett fel i seedfilen och stoppar allt."""
        schema = BLOCK_EDIT_SCHEMA[block_type]
        where = f'"{slug}", block {order} ({block_type})'
        field_specs = schema["fields"]
        list_specs = schema.get("lists", [])
        field_keys = {spec["key"] for spec in field_specs}
        list_keys = {lst["key"] for lst in list_specs}

        unknown = _flat_keys(raw, field_keys | list_keys) - field_keys - list_keys
        if unknown:
            raise CommandError(f"{where}: okända nycklar {', '.join(sorted(unknown))}.")

        values = {}
        for spec in field_specs:
            value = _get_nested(raw, spec["key"])
            values[spec["key"]] = _as_link_input(spec, "" if value is _MISSING else value)

        lists = {}
        for lst in list_specs:
            rows = raw.get(lst["key"], [])
            if not isinstance(rows, list):
                raise CommandError(f"{where}: {lst['key']} ska vara en lista.")
            lists[lst["key"]] = [_row_link_inputs(lst, row) for row in rows]

        try:
            data = clean_block_values(block_type, {}, values)
            return clean_block_rows(block_type, data, lists)
        except KeyError as exc:
            raise CommandError(f"{where}: {exc}") from exc


def _flat_keys(data, declared, prefix=""):
    """Nästlade nycklar som punktnotation ("primary.url"), men bara ned till
    en deklarerad nyckel: en länkbeskrivare under den räknas inte isär."""
    keys = set()
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if dotted in declared or not isinstance(value, dict):
            keys.add(dotted)
        else:
            keys |= _flat_keys(value, declared, f"{dotted}.")
    return keys


def _get_nested(data, dotted):
    current = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _as_link_input(spec, value):
    """Länkväljarens dolda input bär JSON; en beskrivare i seedfilen skickas
    därför som JSON-sträng. En rå sökväg ("/flamingo/app/") går som den är."""
    if spec["type"] == "link" and isinstance(value, dict):
        return json.dumps(value)
    return value


def _row_link_inputs(lst, row):
    if not isinstance(row, dict):
        return row
    specs = {f["key"]: f for f in lst["fields"]}
    return {
        key: _as_link_input(specs[key], value) if key in specs else value
        for key, value in row.items()
    }
