"""
Import site CONTENT previously captured by ``export_site_data`` - load the
fixture into the database and copy the bundled media files into MEDIA_ROOT.

Run this on the server AFTER a ``git pull`` has brought the updated
``seed_data/`` folder onto the box.

Safety:
  * Only content models are touched (the fixture contains no auth/session
    rows), so the production superuser and login sessions are untouched.
  * ``loaddata`` preserves primary keys, so re-running updates the same rows
    instead of creating duplicates (idempotent).
  * Media files are copied, never deleted - an old file that is no longer
    referenced simply stops being used.

By default this REPLACES the current content rows (the customer's live edits)
with whatever is in the fixture. That is almost always what you want for an
initial production seed, but it is destructive, so it must be confirmed:

    uv run python manage.py import_site_data            # interactive confirm
    uv run python manage.py import_site_data --noinput  # for scripts
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Import site content (DB + media) from the tracked seed_data/ folder."

    def add_arguments(self, parser):
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not prompt for confirmation (for use in scripts).",
        )

    def handle(self, *args, **options):
        seed_dir = Path(settings.BASE_DIR) / "seed_data"
        fixture_path = seed_dir / "site_content.json"

        if not fixture_path.exists():
            raise CommandError(
                f"No fixture at {fixture_path}. Did you run export_site_data and "
                "git pull the seed_data/ folder onto this server?"
            )

        if options["interactive"]:
            self.stdout.write(
                self.style.WARNING(
                    "This REPLACES current site content (pages, blocks, menus, "
                    "services) with the contents of seed_data/site_content.json.\n"
                    "Uploaded media files will be copied in. Auth users are NOT "
                    "touched."
                )
            )
            answer = input("Type 'yes' to continue: ")
            if answer.strip().lower() != "yes":
                raise CommandError("Aborted.")

        # 1. Copy bundled media into MEDIA_ROOT first, so rows that reference
        #    them resolve to real files immediately after loaddata.
        media_src = seed_dir / "media"
        media_root = Path(settings.MEDIA_ROOT)
        copied = 0
        if media_src.exists():
            for src in media_src.rglob("*"):
                if src.is_file():
                    rel = src.relative_to(seed_dir)  # e.g. "media/element.jpg"
                    dst = media_root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
        self.stdout.write(f"Copied {copied} media file(s) into {media_root}")

        # 2. Load the content fixture (preserves PKs -> idempotent upsert).
        self.stdout.write("Loading seed_data/site_content.json ...")
        call_command("loaddata", str(fixture_path))

        # 3. loaddata går förbi sanerarna - normalisera så att AI-typografi
        #    ur fixturen aldrig blir liggande i databasen.
        call_command("normalize_typography")

        self.stdout.write(self.style.SUCCESS("Site content imported."))
