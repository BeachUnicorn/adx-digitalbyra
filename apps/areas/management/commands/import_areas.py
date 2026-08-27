"""
Import serviceområden from a JSON file.

    manage.py import_areas seed_data/areas.json
    manage.py import_areas seed_data/areas.json --update
    manage.py import_areas seed_data/areas.json --dry-run

Additive by default, matching the other importers in this project: an area
that already exists (matched on slug) is left exactly as it is, so re-running
the file after the customer has written their own copy is safe. `--update`
opts into overwriting the text fields, and never touches `is_active` - that
flag is the customer's, not the file's.

Shape:

    {"areas": [{"name": "Stockholms län", "level": "region", "children": [...]}]}

Each node accepts: name, slug, level, is_active, heading, intro, body,
meta_title, meta_description, faq [{question, answer}], neighbours [slug],
children [node]. Neighbour links are resolved in a second pass, so a file can
reference areas that appear later in it.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from apps.areas.models import Area, AreaFAQ, AreaLevel
from apps.common.security import sanitize_plain_text, sanitize_rich_html_basic

#: Plain-text fields and their model max_length. Everything imported goes
#: through the same sanitisers the admin form uses - that strips markup and
#: normalises the em dashes, curly quotes and ellipses that give AI-written
#: copy away. A JSON file is just another untrusted input.
PLAIN_FIELDS = {
    "heading": 200,
    "intro": 300,
    "meta_title": 70,
    "meta_description": 200,
}
RICH_FIELDS = ("body",)


class Command(BaseCommand):
    help = "Importera län, kommuner och orter från en JSON-fil."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Sökväg till JSON-filen.")
        parser.add_argument(
            "--update",
            action="store_true",
            help="Skriv över text på områden som redan finns (rör inte Aktiv).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Visa vad som skulle hända utan att spara.",
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"Hittar inte filen: {path}")

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Ogiltig JSON: {exc}") from exc

        nodes = payload.get("areas")
        updates = payload.get("text_updates")
        if not isinstance(nodes, list) and not isinstance(updates, list):
            raise CommandError("JSON måste innehålla en lista under 'areas' eller 'text_updates'.")

        self.update = options["update"]
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.faq_added = 0
        self.missing = 0
        self.neighbour_links = []

        # Hela importen blir en revision med källa IMPORT - syns i historiken
        # och kan återställas som en enhet.
        import reversion

        from apps.assistant.models import RevisionMeta

        with transaction.atomic(), reversion.create_revision():
            reversion.set_comment(f"Import: {path.name}")
            reversion.add_meta(RevisionMeta, source=RevisionMeta.Source.IMPORT)
            for node in nodes or []:
                self._import_node(node, parent=None)
            self._link_neighbours()
            self._apply_text_updates(updates or [])

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry-run: inget sparades."))

        summary = (
            f"Klart. {self.created} skapade, {self.updated} uppdaterade, "
            f"{self.skipped} oförändrade, {self.faq_added} frågor tillagda."
        )
        if self.missing:
            summary += f" {self.missing} slugs i 'text_updates' saknade motsvarande område."
        self.stdout.write(self.style.SUCCESS(summary))

    def _apply_text_updates(self, rows):
        """
        Text-only updates matched on slug.

        Separate from the `areas` tree on purpose: this touches nothing but the
        copy. Level, parent and `is_active` are left alone, so a file of
        rewritten local texts can never reshuffle the hierarchy or republish
        something the customer has deliberately hidden.

        Because it only ever matches existing areas, a text file is useless on
        its own - the tree has to be imported first. Nothing matching at all
        almost always means that step was skipped, so that case is an error
        with an explanation rather than several hundred lines of noise.
        """
        if not rows:
            return

        matched = 0
        unmatched = []
        for row in rows:
            slug = (row.get("slug") or "").strip()
            area = Area.objects.filter(slug=slug).first() if slug else None
            if area is None:
                unmatched.append(slug or "(tom)")
                continue
            matched += 1
            # Without --update a text update only fills fields that are still
            # empty. On the first production run everything is empty, so the
            # whole file lands; on a re-run a text the customer has rewritten
            # is left alone. Overwriting is opt-in, never the default.
            changed = self._apply_text(area, row, only_empty=not self.update)
            if changed:
                area.save()
                self.updated += 1
            else:
                self.skipped += 1
            if row.get("faq"):
                self._import_faq(area, row, replace=self.update)

        if matched == 0:
            raise CommandError(
                f"Ingen av de {len(rows)} slugs i 'text_updates' finns i databasen.\n"
                "Den här filen innehåller bara text och skapar inga områden - den "
                "måste köras EFTER att områdena importerats.\n\n"
                "Kör först:\n"
                "  manage.py import_areas seed_data/areas.json\n"
                "och därefter den här filen igen."
            )

        self.missing = len(unmatched)
        if unmatched:
            preview = ", ".join(unmatched[:10])
            if len(unmatched) > 10:
                preview += f", ... (+{len(unmatched) - 10} till)"
            self.stderr.write(f"Slugs utan matchande område: {preview}")

    # ------------------------------------------------------------------

    def _import_node(self, node, parent):
        name = (node.get("name") or "").strip()
        if not name:
            self.stderr.write("Hoppar över ett område utan namn.")
            return None

        level = node.get("level") or self._infer_level(parent)
        if level not in AreaLevel.values:
            self.stderr.write(f"Okänd nivå '{level}' för {name} - hoppar över.")
            return None

        slug = (node.get("slug") or "").strip() or slugify(name)
        area = Area.objects.filter(slug=slug).first()
        created_now = area is None

        if area is None:
            area = Area(slug=slug, name=name, level=level, parent=parent)
            area.is_active = bool(node.get("is_active", True))
            self._apply_text(area, node)
            area.order = int(node.get("order", 0) or 0)
            area.save()
            self.created += 1
            self._import_faq(area, node, replace=False)
        elif self.update:
            area.name = name
            area.level = level
            area.parent = parent
            self._apply_text(area, node)
            area.save()
            self.updated += 1
            self._import_faq(area, node, replace=True)
        else:
            self.skipped += 1

        # Only link neighbours for areas this run actually created (or is
        # explicitly updating). Doing it unconditionally would silently re-add
        # links the customer had removed by hand on an earlier pass.
        if created_now or self.update:
            for slug_ref in node.get("neighbours") or []:
                self.neighbour_links.append((area.pk, slug_ref))

        for child in node.get("children") or []:
            self._import_node(child, parent=area)

        return area

    def _apply_text(self, area, node, only_empty=False):
        """
        Copy the text fields from `node` onto `area`. Returns True if anything
        changed. With `only_empty` a field that already holds content is left
        as it is - that is what makes a re-run safe against hand-written copy.
        """
        changed = False
        fields = [(f, limit) for f, limit in PLAIN_FIELDS.items()] + [
            (f, None) for f in RICH_FIELDS
        ]
        for field, limit in fields:
            if field not in node:
                continue
            if only_empty and (getattr(area, field) or "").strip():
                continue
            if limit is None:
                value = sanitize_rich_html_basic(node.get(field))
            else:
                value = sanitize_plain_text(node.get(field), max_length=limit)
            if value != getattr(area, field):
                setattr(area, field, value)
                changed = True
        return changed

    def _import_faq(self, area, node, replace):
        rows = node.get("faq") or []
        if not rows:
            return
        if replace:
            area.faq_items.all().delete()
        elif area.faq_items.exists():
            return
        for order, row in enumerate(rows):
            question = sanitize_plain_text(row.get("question"), max_length=300)
            answer = sanitize_rich_html_basic(row.get("answer"))
            if not question or not answer:
                continue
            AreaFAQ.objects.create(area=area, question=question, answer=answer, order=order)
            self.faq_added += 1

    def _link_neighbours(self):
        """Second pass - by now every slug in the file exists."""
        by_slug = {a.slug: a for a in Area.objects.all()}
        for area_pk, slug_ref in self.neighbour_links:
            target = by_slug.get(slug_ref)
            if target is None or target.pk == area_pk:
                continue
            Area.objects.get(pk=area_pk).neighbours.add(target)

    @staticmethod
    def _infer_level(parent):
        if parent is None:
            return AreaLevel.REGION
        if parent.level == AreaLevel.REGION:
            return AreaLevel.MUNICIPALITY
        return AreaLevel.DISTRICT
