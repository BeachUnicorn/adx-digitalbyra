"""
Normalisera AI-typografi (tankstreck, typografiska citattecken, ellipsis) i
ALLT lagrat innehåll - samma teckenkarta som redigeringsvägens sanerare
(apps/common/security.py).

Behövs för innehåll som aldrig passerat ett sparformulär: seedat innehåll
(seed_site), fixture-importer (import_site_data) och databaskloner. De
kommandona anropar detta automatiskt; det kan också köras för hand:

    uv run python manage.py normalize_typography            # rapporterar + rättar
    uv run python manage.py normalize_typography --dry-run  # rapporterar bara

Idempotent. Rör bara innehållsapparna (website, services, areas, faq) -
aldrig auth, sessioner, förfrågningar (kundinskickad historik) eller analytics.
"""

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand
from django.db import models, transaction

from apps.common.security import normalize_json, normalize_typography

CONTENT_APPS = ["website", "services", "areas", "faq"]


class Command(BaseCommand):
    help = "Ersätt AI-typografiska tecken i allt lagrat sajtinnehåll."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Visa vad som skulle ändras utan att skriva något.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        total_rows = 0

        with transaction.atomic():
            for app_label in CONTENT_APPS:
                for model in django_apps.get_app_config(app_label).get_models():
                    changed_rows = self._normalize_model(model, dry)
                    total_rows += changed_rows
                    if changed_rows:
                        self.stdout.write(
                            f"{model._meta.label}: {changed_rows} rad(er) normaliserade"
                        )
            if dry:
                transaction.set_rollback(True)

        verb = "skulle normaliseras" if dry else "normaliserade"
        self.stdout.write(self.style.SUCCESS(f"Klart: {total_rows} rad(er) {verb}."))

    def _normalize_model(self, model, dry):
        text_fields = [
            f
            for f in model._meta.get_fields()
            if isinstance(f, (models.CharField, models.TextField))
            and not isinstance(f, models.JSONField)
            and f.editable
            and not f.choices  # choice-VÄRDEN är nycklar, inte visningstext
        ]
        json_fields = [f for f in model._meta.get_fields() if isinstance(f, models.JSONField)]
        if not text_fields and not json_fields:
            return 0

        changed_rows = 0
        for obj in model.objects.all():
            dirty = []
            for f in text_fields:
                old = getattr(obj, f.name)
                new = normalize_typography(old)
                if new != old:
                    setattr(obj, f.name, new)
                    dirty.append(f.name)
            for f in json_fields:
                old = getattr(obj, f.name)
                new = normalize_json(old)
                if new != old:
                    setattr(obj, f.name, new)
                    dirty.append(f.name)
            if dirty:
                changed_rows += 1
                if not dry:
                    obj.save(update_fields=dirty)
        return changed_rows
