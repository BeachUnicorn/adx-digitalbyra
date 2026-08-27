"""
Export the current site CONTENT (database rows + referenced media files) into
the git-tracked ``seed_data/`` folder, so it can be shipped to another
environment (e.g. production) with a normal ``git push`` / ``git pull``.

What it writes (all under ``<repo>/seed_data/``):
  * ``site_content.json`` - a Django fixture of the content models only
    (website + services). Auth users, sessions, admin logs and content types
    are deliberately excluded so importing never touches the prod superuser.
  * ``media/...``          - a copy of every file referenced by a MediaFile
    row, preserving its path relative to MEDIA_ROOT.

Counterpart: ``import_site_data`` (run on the server after ``git pull``).

Usage (locally, when your dev DB holds the content you want live):
    uv run python manage.py export_site_data
    git add seed_data && git commit -m "Update site seed data" && git push
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.website.models import MediaFile

# Order matters for loaddata: a referenced row must be created before the row
# that points at it (FKs and M2M are resolved as each object is saved).
#   MediaFile      <- referenced by blocks, services, site settings
#   BlockPage      <- referenced by Block, MenuItem, SiteSettings.homepage
#   Menu           <- referenced by MenuItem
#   ServiceCategory/Audience <- referenced by Service (Audience via M2M)
#   SiteSettings   last (points at MediaFile + BlockPage)
CONTENT_MODELS = [
    "website.MediaFile",
    "website.BlockPage",
    "website.Block",
    "website.Menu",
    "website.MenuItem",
    "services.ServiceCategory",
    "services.Audience",
    "services.Service",
    "services.ServiceStep",
    "website.SiteSettings",
]


class Command(BaseCommand):
    help = "Export site content (DB + media) into the tracked seed_data/ folder."

    def handle(self, *args, **options):
        seed_dir = Path(settings.BASE_DIR) / "seed_data"
        fixture_path = seed_dir / "site_content.json"
        seed_dir.mkdir(parents=True, exist_ok=True)

        # 1. Dump the content models to a UTF-8, indented fixture.
        self.stdout.write("Dumping content models -> seed_data/site_content.json")
        with open(fixture_path, "w", encoding="utf-8") as fh:
            call_command(
                "dumpdata",
                *CONTENT_MODELS,
                indent=2,
                stdout=fh,
            )

        # 2. Copy every file referenced by a MediaFile into seed_data/media/,
        #    keeping the same path relative to MEDIA_ROOT (e.g. media/foo.jpg).
        media_root = Path(settings.MEDIA_ROOT)
        copied, missing = 0, 0
        for mf in MediaFile.objects.all():
            name = mf.file.name  # e.g. "media/element.jpg"
            if not name:
                continue
            src = media_root / name
            dst = seed_dir / name
            if not src.exists():
                self.stderr.write(f"  ! missing file on disk, skipping: {src}")
                missing += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {MediaFile.objects.count()} media rows "
                f"({copied} files copied, {missing} missing). "
                "Commit the seed_data/ folder and push."
            )
        )
